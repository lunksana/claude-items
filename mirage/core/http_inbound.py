"""
HTTP / Mixed inbound

  - HTTP/1.1 CONNECT 隧道（HTTPS 网站走代理的标准方式）
  - HTTP/1.1 forward proxy（GET http://... 等绝对 URL）
  - Mixed：peek 第一字节自动分发 SOCKS5 / HTTP/1.1

不实现（设计取舍）：
  - 代理本身 TLS termination（要 cert 配置，rare 场景）
  - Proxy-Authorization 鉴权（127.0.0.1 用例不需要；LAN 共享时再加）
  - HTTP forward 的 keep-alive 多请求循环：首请求转发后切字节中继，后续若到
    不同 upstream 会失败。浏览器对 HTTP forward 极少 keep-alive（不同 host
    自然要新 TCP），可接受
"""

from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

from .sniffer import PrefixedReader
from .utils import get_logger, safe_close

logger = get_logger("http_inbound")

# 上限：request line 8KB，header 行 8KB，header 总数 100
_MAX_REQUEST_LINE = 8192
_MAX_HEADER_LINE = 8192
_MAX_HEADERS = 100
_READ_TIMEOUT = 30.0       # 客户端 idle 等待请求的硬上限

# HTTP/1.1 方法的首字符（涵盖标准方法；小写并入是为宽容某些非标客户端）
_HTTP_METHOD_FIRST = set(b"CGPHDOTAcgphdota")
# 各 byte 含义：C(ONNECT) G(ET) P(OST/UT/ATCH) H(EAD) D(ELETE) O(PTIONS) T(RACE) A(CL)


# ============================================================
# Mixed：peek 一字节决定协议
# ============================================================

async def handle_mixed_connection(reader, writer, outbounds, router, udp_cfg,
                                  registry=None, routing_cache=None) -> None:
    """
    分发：
      \\x05                 → SOCKS5（client.handle_local_connection）
      ASCII (C/G/P/H/D/O/T/A) → HTTP/1.1（handle_http_connection）
      \\x16                 → TLS ClientHello → 回 400 提示（误把代理当 HTTPS proxy）
      其他                  → 关
    """
    try:
        first = await asyncio.wait_for(reader.readexactly(1), timeout=_READ_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        await safe_close(writer)
        return

    prefixed = PrefixedReader(reader, first)

    if first == b"\x05":
        # 延迟 import 防 client → core → client 循环
        from client import handle_local_connection
        await handle_local_connection(prefixed, writer, outbounds, router, udp_cfg,
                                      registry=registry, routing_cache=routing_cache)
        return

    if first[0] in _HTTP_METHOD_FIRST:
        await handle_http_connection(prefixed, writer, outbounds, router, udp_cfg,
                                     registry=registry, routing_cache=routing_cache)
        return

    if first == b"\x16":
        msg = (b"HTTP/1.1 400 Bad Request\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n"
               b"Content-Length: 78\r\n"
               b"Connection: close\r\n"
               b"\r\n"
               b"Proxy does not terminate TLS. Use http:// or socks5:// scheme, not https://")
        try:
            writer.write(msg)
            await writer.drain()
        except Exception:
            pass
        await safe_close(writer)
        return

    logger.debug("mixed: unknown first byte 0x%02x, closing", first[0])
    await safe_close(writer)


# ============================================================
# HTTP/1.1 inbound
# ============================================================

async def handle_http_connection(reader, writer, outbounds, router, udp_cfg,
                                 registry=None, routing_cache=None) -> None:
    """
    HTTP/1.1 proxy 入口。CONNECT → 字节隧道；其他方法 → forward 改写后中继。
    """
    # ── 1) 读 request line ──
    try:
        request_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_READ_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        await safe_close(writer)
        return
    except asyncio.LimitOverrunError:
        await _send_status(writer, 414, "Request-URI Too Long")
        await safe_close(writer)
        return
    if len(request_line) > _MAX_REQUEST_LINE:
        await _send_status(writer, 414, "Request-URI Too Long")
        await safe_close(writer)
        return

    try:
        method, target, version = request_line.decode("ascii").rstrip("\r\n").split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        await _send_status(writer, 400, "Bad Request")
        await safe_close(writer)
        return

    if not version.startswith("HTTP/1."):
        await _send_status(writer, 505, "HTTP Version Not Supported")
        await safe_close(writer)
        return

    # ── 2) 读 headers（保序，因为转发时要逐行写回）──
    headers: list[tuple[str, str]] = []
    try:
        for _ in range(_MAX_HEADERS + 1):
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_READ_TIMEOUT)
            if line == b"\r\n":
                break
            if len(line) > _MAX_HEADER_LINE:
                await _send_status(writer, 431, "Request Header Fields Too Large")
                await safe_close(writer)
                return
            try:
                decoded = line.decode("latin-1").rstrip("\r\n")
            except UnicodeDecodeError:
                await _send_status(writer, 400, "Bad Request")
                await safe_close(writer)
                return
            if ":" not in decoded:
                await _send_status(writer, 400, "Malformed header")
                await safe_close(writer)
                return
            name, _, value = decoded.partition(":")
            if not name.strip():
                await _send_status(writer, 400, "Empty header name")
                await safe_close(writer)
                return
            headers.append((name.strip(), value.strip()))
        else:
            await _send_status(writer, 431, "Too many headers")
            await safe_close(writer)
            return
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        await safe_close(writer)
        return

    method = method.upper()

    # ── 3) CONNECT：回 200 后字节中继 ──
    if method == "CONNECT":
        host, port = _parse_authority(target)
        if host is None:
            await _send_status(writer, 400, "CONNECT target must be host:port")
            await safe_close(writer)
            return
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        except Exception:
            await safe_close(writer)
            return
        # 字节中继：等同 SOCKS5 TCP CONNECT
        # _dispatch 是 client.py 的函数，late import 防循环
        from client import _dispatch
        await _dispatch(reader, writer, host, port, outbounds, router,
                        registry=registry, routing_cache=routing_cache,
                        inbound_type="HTTP")
        return

    # ── 4) HTTP forward：target 必须是绝对 URL ──
    target_lower = target.lower()
    if not (target_lower.startswith("http://") or target_lower.startswith("https://")):
        await _send_status(writer, 400, "HTTP forward requires absolute URL")
        await safe_close(writer)
        return

    url = urlparse(target)
    host = url.hostname
    if not host:
        await _send_status(writer, 400, "Invalid URL host")
        await safe_close(writer)
        return
    port = url.port or (443 if url.scheme == "https" else 80)
    path = url.path or "/"
    if url.query:
        path += "?" + url.query

    # 剥 Proxy-* 头（hop-by-hop），改 request line 为相对路径
    filtered = [(k, v) for k, v in headers if not k.lower().startswith("proxy-")]

    rewritten = f"{method} {path} {version}\r\n".encode("ascii")
    for k, v in filtered:
        rewritten += f"{k}: {v}\r\n".encode("latin-1")
    rewritten += b"\r\n"

    # body 仍在 reader 里（按 Content-Length / chunked 由 upstream 自行读），
    # PrefixedReader 先吐 rewritten，再 fall through 到原 reader（含 body）
    prefixed = PrefixedReader(reader, rewritten)
    from client import _dispatch
    await _dispatch(prefixed, writer, host, port, outbounds, router,
                    registry=registry, routing_cache=routing_cache,
                    inbound_type="HTTP")


# ============================================================
# 辅助
# ============================================================

def _parse_authority(target: str) -> tuple[Optional[str], int]:
    """CONNECT host:port 解析；支持 IPv6 字面量 `[::1]:443`。"""
    target = target.strip()
    if target.startswith("["):
        end = target.find("]")
        if end < 0 or not target[end + 1:].startswith(":"):
            return None, 0
        host = target[1:end]
        try:
            port = int(target[end + 2:])
        except ValueError:
            return None, 0
    elif ":" in target:
        host, port_s = target.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            return None, 0
    else:
        return None, 0
    if not host or not (1 <= port <= 65535):
        return None, 0
    return host, port


async def _send_status(writer, code: int, reason: str) -> None:
    msg = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Connection: close\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    ).encode("ascii")
    try:
        writer.write(msg)
        await writer.drain()
    except Exception:
        pass
