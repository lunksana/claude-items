"""
加密信道 —— 用 ChaCha20-Poly1305 对代理数据加密

帧格式（v2，nonce 不上线）：
  [2 字节明文长度] [密文 + 16 字节 Tag]

Nonce 由双方各自维护的计数器派生，不在线路上传输，
每包减少 1 次 readexactly（3→2）和 12 字节开销。

会话密钥 = HKDF(SHA256(password), 随机盐)
"""


from __future__ import annotations

import asyncio

import os

import struct

import hashlib


from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cryptography.hazmat.primitives import hashes

SALT_SIZE  = 16
NONCE_SIZE = 12
TAG_SIZE   = 16

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

    async def do_handshake_as_initiator(self) -> None:
        salt = os.urandom(SALT_SIZE)
        self._writer.write(salt)
        await self._writer.drain()
        self._cipher = ChaCha20Poly1305(derive_session_key(self._password, salt))

    async def do_handshake_as_responder(self) -> None:
        salt = await self._reader.readexactly(SALT_SIZE)
        self._cipher = ChaCha20Poly1305(derive_session_key(self._password, salt))

    # --- 数据读写 ---

    async def send(self, plaintext: bytes) -> None:
        nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
        self._send_nonce += 1
        ciphertext = self._cipher.encrypt(nonce, plaintext, None)
        # nonce 不上线，线路格式：[2字节明文长度][密文+tag]
        self._writer.write(struct.pack("!H", len(plaintext)) + ciphertext)
        # 仅在写缓冲积压时 drain，避免每包都触发协程切换
        if self._writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
            await self._writer.drain()

    async def recv(self) -> bytes:
        # 一次读取 2 字节头部（之前是 3 次 readexactly，现在是 2 次）
        header = await self._reader.readexactly(2)
        plain_len = struct.unpack("!H", header)[0]
        ciphertext = await self._reader.readexactly(plain_len + TAG_SIZE)
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
