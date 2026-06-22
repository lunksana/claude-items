"""
加密信道 —— 用 ChaCha20-Poly1305 对代理数据加密

帧格式（v3，TLS 应用数据记录）：
  [1字节 0x17] [2字节 0x03 0x03] [2字节密文长度] [密文 + 16字节 Tag]

与 TLS 1.2 application data record 格式完全一致，握手完成后的流量
在旁观者眼中不可与真实 HTTPS 区分。

Nonce 由双方各自维护的计数器派生，不在线路上传输。

会话密钥 = HKDF(SHA256(password), salt=client_random)
  client_random 来自 ClientHello，双方均已知，无需额外传输。
"""


from __future__ import annotations

import asyncio

import random

import struct

import time

import hashlib


from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

from cryptography.hazmat.primitives import hashes

NONCE_SIZE = 12
TAG_SIZE   = 16
_MAX_RECORD = 16384  # TLS 单条记录最大明文长度（RFC 5246 §6.2.1）

# 写缓冲超过此阈值才 drain，避免每包都切换协程
_DRAIN_THRESHOLD = 64 * 1024

# 反指纹 record 分片：真实 HTTPS 应用层数据大小分布是混合的，不会清一色 16KB 顶满
# 三档：50% 大块（追求吞吐）、35% 中块、15% 小块（HTTP/1.1 chunked / HTTP/2 frame）
_RECORD_SIZE_BUCKETS = ((16384, 0.50), (8192, 0.35), (4096, 0.15))


def _derive_master(password: str, salt: bytes) -> bytes:
    raw_key = hashlib.sha256(password.encode()).digest()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"mirage-session")
    return hkdf.derive(raw_key)


def _expand(master: bytes, info: bytes) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=info).derive(master)


class EncryptedTunnel:
    """
    包装 asyncio StreamReader/StreamWriter，提供加密读写。

    握手：
      发送方先发 16 字节随机盐 → 接收方用同一盐派生密钥。
      双方各自维护独立的 nonce 计数器（发送/接收分开）。
    """

    __slots__ = ("_reader", "_writer", "_password", "_send_cipher", "_recv_cipher",
                 "_send_nonce", "_recv_nonce")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, password: str):
        self._reader = reader
        self._writer = writer
        self._password = password
        self._send_cipher: ChaCha20Poly1305 | None = None
        self._recv_cipher: ChaCha20Poly1305 | None = None
        self._send_nonce = 0
        self._recv_nonce = 0

    # --- 握手 ---

    async def do_handshake_as_initiator(self, client_random: bytes) -> None:
        """派生会话密钥。client_random 来自已发出的 ClientHello，无需额外传输。"""
        master = _derive_master(self._password, client_random)
        # 方向独立密钥：避免双向共用同一 (key, nonce) 对导致的 nonce 复用攻击
        self._send_cipher = ChaCha20Poly1305(_expand(master, b"c2s"))
        self._recv_cipher = ChaCha20Poly1305(_expand(master, b"s2c"))

    async def do_handshake_as_responder(self, client_random: bytes) -> None:
        """派生会话密钥。client_random 由调用方从 ClientHello 中提取。"""
        master = _derive_master(self._password, client_random)
        self._send_cipher = ChaCha20Poly1305(_expand(master, b"s2c"))
        self._recv_cipher = ChaCha20Poly1305(_expand(master, b"c2s"))

    # --- 数据读写 ---

    @staticmethod
    def _next_record_size() -> int:
        """按 _RECORD_SIZE_BUCKETS 分布随机选一档 record 上限，模拟真实 HTTPS 流量"""
        r = random.random()
        cum = 0.0
        for size, weight in _RECORD_SIZE_BUCKETS:
            cum += weight
            if r <= cum:
                return size
        return _MAX_RECORD

    async def send(self, plaintext: bytes) -> None:
        """
        发送加密应用数据（完整 TLS 1.3 inner content type 模拟）。

        线路格式：
            outer: 0x17 0x03 0x03 [2B len] [ciphertext]
            plaintext_for_AEAD = data || 0x17   ← inner content type = application_data
            （不加 padding；padding 用于 record size 隐藏，未来可扩）

        record 大小用分桶随机化，避免清一色 16KB 顶满成为指纹特征。
        """
        off = 0
        while off < len(plaintext):
            limit = self._next_record_size()
            chunk = plaintext[off:off + limit]
            off  += len(chunk)
            nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
            self._send_nonce += 1
            # 末尾追加 inner content type 0x17 (application_data) —— RFC 8446 §5.2
            inner = chunk + b"\x17"
            ciphertext = self._send_cipher.encrypt(nonce, inner, None)
            self._writer.write(b"\x17\x03\x03" + struct.pack("!H", len(ciphertext)) + ciphertext)
        if self._writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
            await self._writer.drain()

    async def send_close_notify(self) -> None:
        """
        发一帧加密的 TLS 1.3 close_notify alert，与 Chrome/Firefox 关闭连接产生的
        字节序列**逐字节一致**：

            outer: 0x17 0x03 0x03 0x00 0x13 [19B ciphertext]
            plaintext_for_AEAD = 0x01 0x00 0x15
                                 (level=warning, desc=close_notify, inner_type=alert)

        失败静默（对端已关 / writer 已断）。
        """
        try:
            if self._send_cipher is None or self._writer.is_closing():
                return
            nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
            self._send_nonce += 1
            # TLS 1.3 Alert: [level=1 warning][desc=0 close_notify][inner_type=0x15]
            inner = b"\x01\x00\x15"
            ciphertext = self._send_cipher.encrypt(nonce, inner, None)
            # 外层 = 0x17（application_data）—— TLS 1.3 加密 alert 的外层必须如此
            self._writer.write(b"\x17\x03\x03" + struct.pack("!H", len(ciphertext)) + ciphertext)
            await self._writer.drain()
        except (OSError, EOFError, asyncio.CancelledError):
            pass

    async def recv(self) -> bytes:
        """
        接收并解密一个 TLS 1.3 风格记录。

        外层无论数据还是 alert 都是 0x17（application_data）—— TLS 1.3 加密记录
        外层一律是 application_data。真实 content type 在 plaintext 末尾。
        """
        header = await self._reader.readexactly(5)
        length = int.from_bytes(header[3:5], "big")
        ciphertext = await self._reader.readexactly(length)
        nonce = self._recv_nonce.to_bytes(NONCE_SIZE, "big")
        self._recv_nonce += 1
        plaintext = self._recv_cipher.decrypt(nonce, ciphertext, None)

        if not plaintext:
            # 空 plaintext 不合规（缺 inner type），按对端关闭处理
            raise EOFError("empty plaintext (missing inner content type)")

        inner_type = plaintext[-1]
        body       = plaintext[:-1]
        if inner_type == 0x17:           # application_data
            return body
        if inner_type == 0x15:           # alert (close_notify 等)
            raise EOFError("peer sent TLS alert (close_notify)")
        raise ValueError(f"unknown TLS inner content type {inner_type:#x}")

    async def drain_recv(self, max_seconds: float = 0.5) -> None:
        """
        在 close 之前 best-effort 读光底层 reader 的接收缓冲，避免 close → RST。

        不走 recv()/AEAD 解密路径：对端在 grace 超时被 cancel 的场景下，可能
        还在推送加密 record，我们不需要解密、只需要从 OS TCP 接收缓冲拿走，
        让后续 close() 看到的接收缓冲为空。

        硬上限 `max_seconds`：对端无限推时不能无限读。读到 EOF 或超时即返。
        失败静默：任何异常都不打断后续 close 流程。
        """
        try:
            deadline = time.monotonic() + max_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                chunk = await asyncio.wait_for(self._reader.read(65536), timeout=remaining)
                if not chunk:
                    return  # EOF
        except Exception:
            pass

    async def relay_with(self, other: "EncryptedTunnel") -> None:
        """与另一个加密隧道双向中继"""
        async def forward(src: "EncryptedTunnel", dst: "EncryptedTunnel"):
            try:
                while True:
                    data = await src.recv()
                    await dst.send(data)
            except Exception:
                pass

        task_a = asyncio.create_task(forward(self, other))
        task_b = asyncio.create_task(forward(other, self))
        try:
            await asyncio.wait([task_a, task_b], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (task_a, task_b):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
