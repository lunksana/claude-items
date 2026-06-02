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
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"pyrealiy-session")
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
        # record 大小用分桶随机化，避免清一色 16KB 顶满成为指纹特征
        off = 0
        while off < len(plaintext):
            limit = self._next_record_size()
            chunk = plaintext[off:off + limit]
            off  += len(chunk)
            nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
            self._send_nonce += 1
            ciphertext = self._send_cipher.encrypt(nonce, chunk, None)
            # TLS application data record: type=0x17, version=TLS1.2, length=len(ciphertext)
            # 合并 header + ciphertext 一次 write（少一次 syscall）
            self._writer.write(b"\x17\x03\x03" + struct.pack("!H", len(ciphertext)) + ciphertext)
        if self._writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
            await self._writer.drain()

    async def send_close_notify(self) -> None:
        """
        发一帧加密的 TLS Alert(close_notify)，让对端看到"规范的 TLS 关闭"。
        Alert content_type = 0x15，明文 body = [level=1 warning, desc=0 close_notify]。
        失败静默（对端已关 / writer 已断）。
        """
        try:
            if self._send_cipher is None or self._writer.is_closing():
                return
            nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
            self._send_nonce += 1
            ciphertext = self._send_cipher.encrypt(nonce, b"\x01\x00", None)
            # Alert record: content_type=0x15
            self._writer.write(b"\x15\x03\x03" + struct.pack("!H", len(ciphertext)) + ciphertext)
            await self._writer.drain()
        except Exception:
            pass

    async def recv(self) -> bytes:
        # TLS record header: content_type(1) + version(2) + length(2)
        header = await self._reader.readexactly(5)
        content_type = header[0]
        length = int.from_bytes(header[3:5], "big")
        ciphertext = await self._reader.readexactly(length)
        nonce = self._recv_nonce.to_bytes(NONCE_SIZE, "big")
        self._recv_nonce += 1
        plaintext = self._recv_cipher.decrypt(nonce, ciphertext, None)
        if content_type == 0x15:
            # TLS Alert（close_notify 等）：当 EOF 处理。relay/feed 路径都把这当结束信号
            raise EOFError("peer sent TLS alert (close_notify)")
        return plaintext

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
