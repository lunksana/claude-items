"""
在 EncryptedTunnel 上跑标准 TLS（用 ssl.MemoryBIO）。

为什么需要：mirage 隧道是 message 模式（每次 send / recv 一个 AEAD 帧），不能直接
喂给 `ssl.SSLContext.wrap_socket` —— 后者要真 socket。MemoryBIO 模式允许我们手工
拉送 SSL 状态机的入/出字节，把两边的 byte 流用 tunnel.send / tunnel.recv 接力。

适用：DoT（端口 853）、DoH（端口 443）等需要标准 TLS 1.2+/1.3 与公共服务器对话的
场景。

设计取舍：
  - 仅做 TLS 客户端（mirage 永远是 client 这一侧）
  - server_hostname 用调用方提供的字符串（用于 SNI + 证书校验）
  - 默认走系统 CA + 默认 hostname check；调用方需要时可注入自定义 SSLContext
  - 不带读超时（DNS 上层调用方自己有 timeout）
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Optional


class TlsOverTunnel:
    """
    用法：
        tls = TlsOverTunnel(tunnel, server_hostname="1.1.1.1")
        await tls.do_handshake()
        await tls.send(b"...")
        data = await tls.recv()
    """

    def __init__(self, tunnel, server_hostname: str, ctx: Optional[ssl.SSLContext] = None,
                 alpn: Optional[list[str]] = None):
        self._tunnel = tunnel
        self._in_bio = ssl.MemoryBIO()
        self._out_bio = ssl.MemoryBIO()
        ssl_ctx = ctx or ssl.create_default_context()
        if alpn:
            try:
                ssl_ctx.set_alpn_protocols(alpn)
            except NotImplementedError:
                pass
        self._ssl = ssl_ctx.wrap_bio(self._in_bio, self._out_bio,
                                     server_hostname=server_hostname)

    async def do_handshake(self) -> None:
        """完成 TLS 握手。失败抛 ssl.SSLError。"""
        while True:
            try:
                self._ssl.do_handshake()
                await self._flush_out()
                return
            except ssl.SSLWantReadError:
                await self._flush_out()
                await self._read_into_bio()
            except ssl.SSLWantWriteError:
                await self._flush_out()

    async def send(self, data: bytes) -> None:
        """写应用数据（一次或多次写满 data）。"""
        view = memoryview(data)
        while view:
            n = self._ssl.write(view)
            view = view[n:]
            await self._flush_out()

    async def recv(self, max_bytes: int = 16384) -> bytes:
        """
        读应用数据。返回至少 1 字节（除非 TLS 已关闭，抛 EOFError）。
        """
        while True:
            try:
                chunk = self._ssl.read(max_bytes)
                if not chunk:
                    raise EOFError("TLS peer closed")
                return chunk
            except ssl.SSLWantReadError:
                await self._read_into_bio()

    async def recv_exactly(self, n: int) -> bytes:
        """读够 n 字节（少了 → EOFError）。"""
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.recv(n - len(buf))
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        """发 close_notify。不阻塞，调用方负责后续 tunnel.send_close_notify。"""
        try:
            self._ssl.unwrap()
        except ssl.SSLWantReadError:
            pass
        except Exception:
            pass

    # ── 内部 ────────────────────────────────────────────────────────────────

    async def _flush_out(self) -> None:
        """把 out_bio 里所有待发字节通过 tunnel.send 推走。"""
        data = self._out_bio.read()
        while data:
            await self._tunnel.send(data)
            data = self._out_bio.read()

    async def _read_into_bio(self) -> None:
        """从 tunnel 收一段加密字节注入 in_bio。"""
        chunk = await self._tunnel.recv()
        if not chunk:
            self._in_bio.write_eof()
            return
        self._in_bio.write(chunk)
