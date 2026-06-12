"""
最小 WebSocket 服务端协议（RFC 6455）。

支持的范围（够 Yacd / metacubexd 的 /traffic + /logs 订阅用）：
  - 握手：GET + Upgrade: websocket + Sec-WebSocket-Key/Version → 101 + Accept
  - 服务端发：text 帧（JSON），不分片，单帧最大 ~16MB
  - 服务端收：自动处理 ping → pong、close → close ack；text/binary 客户端帧忽略
  - 关闭：双向 close 帧 + 底层 TCP close

不支持（不在 Clash UI 订阅场景里）：
  - 分片（fin=0 的连续帧）
  - 扩展（permessage-deflate 等）
  - 客户端 → 服务端的应用数据消费
  - WSS（TLS over WS）—— 本身在 127.0.0.1 上，TLS 没意义
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from typing import Optional

from .http_proto import HTTPError, Request

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes
_OP_CONT  = 0x0
_OP_TEXT  = 0x1
_OP_BIN   = 0x2
_OP_CLOSE = 0x8
_OP_PING  = 0x9
_OP_PONG  = 0xA

_MAX_FRAME_PAYLOAD = 1 * 1024 * 1024   # 1MB 单帧上限（来自客户端 ping/close 通常 < 125 字节）


async def perform_handshake(req: Request, writer: asyncio.StreamWriter) -> None:
    """
    GET + Upgrade: websocket → 101 Switching Protocols。

    抛 HTTPError 表示非法握手，让上层回 4xx。
    """
    if req.method != "GET":
        raise HTTPError(405, "WS upgrade requires GET")
    if req.header("upgrade").lower() != "websocket":
        raise HTTPError(400, "missing Upgrade: websocket")
    if "upgrade" not in req.header("connection").lower():
        raise HTTPError(400, "missing Connection: Upgrade")
    if req.header("sec-websocket-version") != "13":
        raise HTTPError(400, "unsupported Sec-WebSocket-Version (need 13)")
    key = req.header("sec-websocket-key").strip()
    if not key:
        raise HTTPError(400, "missing Sec-WebSocket-Key")

    accept = base64.b64encode(hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n"
    )
    await writer.drain()


# ============================================================
# 帧编解码
# ============================================================

def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """构造一个 FIN=1 单帧（服务端不 mask）。"""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """读一个帧 → (fin, opcode, payload)。客户端 mask 自动解。"""
    head = await reader.readexactly(2)
    fin = (head[0] & 0x80) != 0
    opcode = head[0] & 0x0F
    masked = (head[1] & 0x80) != 0
    plen = head[1] & 0x7F
    if plen == 126:
        plen = struct.unpack("!H", await reader.readexactly(2))[0]
    elif plen == 127:
        plen = struct.unpack("!Q", await reader.readexactly(8))[0]
    if plen > _MAX_FRAME_PAYLOAD:
        raise ConnectionError(f"WS frame too large: {plen}")
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(plen) if plen else b""
    if mask:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return fin, opcode, payload


# ============================================================
# WSConnection：handler 拿到这个对象，发 send_text 即可
# ============================================================

class WSConnection:
    """
    用法：
        await perform_handshake(req, writer)
        ws = WSConnection(reader, writer)
        try:
            while not ws.closed:
                await ws.send_text(json.dumps({...}))
                await asyncio.sleep(1)
        finally:
            await ws.close()

    构造时自动启动 _read_loop 处理对端 ping / close。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._send_lock = asyncio.Lock()   # send_text 和 pong 不能交错写
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    async def send_text(self, s: str) -> None:
        if self._closed:
            raise ConnectionError("WS closed")
        await self._send_raw(_OP_TEXT, s.encode("utf-8"))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        body = struct.pack("!H", code) + reason.encode("utf-8")
        try:
            await self._send_raw(_OP_CLOSE, body)
        except Exception:
            pass
        if not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _send_raw(self, opcode: int, payload: bytes) -> None:
        async with self._send_lock:
            try:
                self._writer.write(_encode_frame(opcode, payload))
                await self._writer.drain()
            except Exception:
                self._closed = True
                raise

    async def _read_loop(self) -> None:
        try:
            while True:
                _fin, opcode, payload = await _read_frame(self._reader)
                if opcode == _OP_CLOSE:
                    self._closed = True
                    # 回一个 close ack（spec 要求）
                    try:
                        await self._send_raw(_OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
                    except Exception:
                        pass
                    return
                if opcode == _OP_PING:
                    try:
                        await self._send_raw(_OP_PONG, payload)
                    except Exception:
                        return
                    continue
                # text / binary / pong / continuation：管理 UI 不会发这些，直接忽略
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._closed = True
