"""
加密信道 —— 用 ChaCha20-Poly1305 对代理数据加密

帧格式：
  [2 字节明文长度] [12 字节 Nonce] [密文 + 16 字节 Tag]

会话密钥 = HKDF(HMAC-SHA256(password) + 随机盐)
"""


from __future__ import annotations

import asyncio

import os

import struct

import hashlib


from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cryptography.hazmat.primitives import hashes

SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16


def derive_session_key(password: str, salt: bytes) -> bytes:
    """从密码和随机盐派生 32 字节会话密钥"""
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
        """客户端调用：发送盐，完成密钥协商"""
        salt = os.urandom(SALT_SIZE)
        self._writer.write(salt)
        await self._writer.drain()
        key = derive_session_key(self._password, salt)
        self._cipher = ChaCha20Poly1305(key)

    async def do_handshake_as_responder(self) -> None:
        """服务端调用：接收盐，完成密钥协商"""
        salt = await self._reader.readexactly(SALT_SIZE)
        key = derive_session_key(self._password, salt)
        self._cipher = ChaCha20Poly1305(key)

    # --- 数据读写 ---

    async def send(self, plaintext: bytes) -> None:
        nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
        self._send_nonce += 1
        ciphertext = self._cipher.encrypt(nonce, plaintext, None)
        header = struct.pack("!H", len(plaintext))
        self._writer.write(header + nonce + ciphertext)
        await self._writer.drain()

    async def recv(self) -> bytes:
        header = await self._reader.readexactly(2)
        plain_len = struct.unpack("!H", header)[0]
        nonce = await self._reader.readexactly(NONCE_SIZE)
        cipher_len = plain_len + TAG_SIZE
        ciphertext = await self._reader.readexactly(cipher_len)
        # 用接收方自己的 nonce 计数器验证（防重放）
        expected_nonce = self._recv_nonce.to_bytes(NONCE_SIZE, "big")
        self._recv_nonce += 1
        if nonce != expected_nonce:
            raise ValueError("Nonce mismatch, possible replay attack")
        return self._cipher.decrypt(nonce, ciphertext, None)

    async def relay_with(self, other: "EncryptedTunnel") -> None:
        """与另一个加密隧道双向中继（一端解密后再加密转发）"""
        async def forward(src: "EncryptedTunnel", dst: "EncryptedTunnel"):
            try:
                while True:
                    data = await src.recv()
                    await dst.send(data)
            except Exception:
                pass

        await asyncio.gather(forward(self, other), forward(other, self))
