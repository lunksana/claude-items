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

import struct

import hashlib


from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cryptography.hazmat.primitives import hashes

NONCE_SIZE = 12
TAG_SIZE   = 16
_MAX_RECORD = 16384  # TLS 单条记录最大明文长度（RFC 5246 §6.2.1）

# 写缓冲超过此阈值才 drain，避免每包都切换协程
_DRAIN_THRESHOLD = 64 * 1024


def derive_session_key(password: str, salt: bytes) -> bytes:
    raw_key = hashlib.sha256(password.encode()).digest()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"pyrealiy-session")
    return hkdf.derive(raw_key)


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
        self._cipher: ChaCha20Poly1305 | None = None
        self._send_nonce = 0
        self._recv_nonce = 0

    # --- 握手 ---

    async def do_handshake_as_initiator(self, client_random: bytes) -> None:
        """派生会话密钥。client_random 来自已发出的 ClientHello，无需额外传输。"""
        self._cipher = ChaCha20Poly1305(derive_session_key(self._password, client_random))

    async def do_handshake_as_responder(self, client_random: bytes) -> None:
        """派生会话密钥。client_random 由调用方从 ClientHello 中提取。"""
        self._cipher = ChaCha20Poly1305(derive_session_key(self._password, client_random))

    # --- 数据读写 ---

    async def send(self, plaintext: bytes) -> None:
        # 超过单条 TLS 记录限制时自动分片
        off = 0
        while off < len(plaintext):
            chunk = plaintext[off:off + _MAX_RECORD]
            off  += len(chunk)
            nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
            self._send_nonce += 1
            ciphertext = self._cipher.encrypt(nonce, chunk, None)
            # TLS application data record: type=0x17, version=TLS1.2, length=len(ciphertext)
            self._writer.write(b"\x17\x03\x03" + struct.pack("!H", len(ciphertext)) + ciphertext)
        if self._writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
            await self._writer.drain()

    async def recv(self) -> bytes:
        # TLS record header: content_type(1) + version(2) + length(2)
        header = await self._reader.readexactly(5)
        length = int.from_bytes(header[3:5], "big")
        ciphertext = await self._reader.readexactly(length)
        nonce = self._recv_nonce.to_bytes(NONCE_SIZE, "big")
        self._recv_nonce += 1
        return self._cipher.decrypt(nonce, ciphertext, None)

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
