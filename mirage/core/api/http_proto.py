"""
最小 HTTP/1.1 服务端协议（足够支撑 Clash 兼容 API + WebSocket 升级）。

刻意不支持（不影响 Clash UI）：
  - chunked **请求** body（响应可以 chunked）
  - 100-Continue
  - trailer
  - pipelining（keep-alive 上顺序处理一个 request）
  - HTTP/2

只承担：parse_request / write_response / write_chunked / parse_query。
鉴权 / CORS / 路由都在上层 server.py 处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, unquote

_MAX_REQUEST_LINE = 8192      # method + path + version 上限
_MAX_HEADER_LINE = 8192       # 单行 header 上限
_MAX_HEADERS = 100            # header 行数上限
_MAX_BODY = 1 * 1024 * 1024   # 请求 body 1MB 上限（管理 API 用不到大 body）

# Status code → reason phrase
_REASON = {
    101: "Switching Protocols",
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
}


class HTTPError(Exception):
    """协议级错误。由 server.py 转换为对应 status 响应。"""
    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message or _REASON.get(status, "Error")
        super().__init__(self.message)


@dataclass
class Request:
    method: str
    path: str                          # 不带 query string
    query: dict[str, str]              # query string 解析结果
    headers: dict[str, str]            # 全小写 key
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    extra_headers: list[tuple[str, str]] = field(default_factory=list)
    close_after: bool = False          # True → 响应后断连（如 Connection: close 或致命错误）


async def read_request(reader: asyncio.StreamReader) -> Optional[Request]:
    """
    读一个 HTTP/1.1 请求。EOF / 空连接 → None（调用方应静默关连接，不当错误）。

    抛 HTTPError(status) 表示协议错误，调用方应回对应状态码并关连接。
    """
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError:
        return None  # 对端 EOF / 半关闭
    except asyncio.LimitOverrunError:
        raise HTTPError(413, "request line too long")

    if not line.strip():
        # 连接刚建立就收 \r\n —— 非法但宽容地等下一行
        return None

    if len(line) > _MAX_REQUEST_LINE:
        raise HTTPError(413, "request line too long")

    try:
        method, target, version = line.decode("ascii", errors="strict").rstrip("\r\n").split(" ", 2)
    except ValueError:
        raise HTTPError(400, "malformed request line")

    if not version.startswith("HTTP/1."):
        raise HTTPError(505 if version.startswith("HTTP/") else 400, "only HTTP/1.x supported")

    path, query = _split_target(target)

    # headers
    headers: dict[str, str] = {}
    for _ in range(_MAX_HEADERS + 1):
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.LimitOverrunError:
            raise HTTPError(413, "header line too long")
        except asyncio.IncompleteReadError:
            raise HTTPError(400, "headers truncated")
        if line == b"\r\n":
            break
        if len(line) > _MAX_HEADER_LINE:
            raise HTTPError(413, "header line too long")
        try:
            name, _, value = line.decode("latin-1").rstrip("\r\n").partition(":")
        except UnicodeDecodeError:
            raise HTTPError(400, "header decode error")
        if not name or "_" in name:
            raise HTTPError(400, "invalid header name")
        # 同名 header 用 ", " 合并（RFC 7230）
        key = name.strip().lower()
        val = value.strip()
        if key in headers:
            headers[key] = headers[key] + ", " + val
        else:
            headers[key] = val
    else:
        raise HTTPError(413, "too many headers")

    # body
    body = b""
    cl_str = headers.get("content-length")
    if cl_str:
        try:
            cl = int(cl_str)
        except ValueError:
            raise HTTPError(400, "invalid Content-Length")
        if cl < 0 or cl > _MAX_BODY:
            raise HTTPError(413, "body too large")
        if cl > 0:
            try:
                body = await reader.readexactly(cl)
            except asyncio.IncompleteReadError:
                raise HTTPError(400, "body truncated")
    elif headers.get("transfer-encoding", "").lower() == "chunked":
        raise HTTPError(501, "chunked request body not supported")

    return Request(method=method.upper(), path=path, query=dict(query), headers=headers, body=body)


def _split_target(target: str) -> tuple[str, list[tuple[str, str]]]:
    """`/path?a=1&b=2` → (`/path`, [('a','1'), ('b','2')])"""
    if "?" not in target:
        return unquote(target), []
    p, _, q = target.partition("?")
    return unquote(p), parse_qsl(q, keep_blank_values=True)


async def write_response(writer: asyncio.StreamWriter, resp: Response, *, keep_alive: bool) -> None:
    """写一个完整 HTTP/1.1 响应（status + headers + body）。"""
    reason = _REASON.get(resp.status, "OK")
    out = bytearray(f"HTTP/1.1 {resp.status} {reason}\r\n".encode("ascii"))
    out += f"Content-Type: {resp.content_type}\r\n".encode("ascii")
    out += f"Content-Length: {len(resp.body)}\r\n".encode("ascii")
    if keep_alive and not resp.close_after:
        out += b"Connection: keep-alive\r\n"
    else:
        out += b"Connection: close\r\n"
    for k, v in resp.extra_headers:
        out += f"{k}: {v}\r\n".encode("latin-1")
    out += b"\r\n"
    out += resp.body
    writer.write(out)
    await writer.drain()
