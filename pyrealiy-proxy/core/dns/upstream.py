"""
DNS 上游 resolver：UDP / DoT / DoH 三种 scheme，统一接口 `Upstream.query(data) → bytes`。

地址解析：
  "1.1.1.1:53"                          → UdpUpstream  （走 pyrealiy 隧道 → server 端 plain TCP/UDP 转发）
  "tls://1.1.1.1:853"                   → DotUpstream  （pyrealiy 隧道 → TLS 1.2+ → 长度前缀 DNS）
  "https://1.1.1.1/dns-query"           → DohUpstream  （pyrealiy 隧道 → TLS → HTTP/1.1 POST application/dns-message）

约束：
  - DoH URL 暂只支持 **host = IP literal**（防 bootstrap 死循环：DNS 服务自己需要先解析 DNS 服务器的域名）
  - 所有 upstream 都共用同一条 pyrealiy 隧道做底层；TLS 由客户端做端到端
    （server 端只是 pass-through TCP）
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import struct
from typing import Optional
from urllib.parse import urlparse

from ..utils import pack_address, get_logger
from .tls_over_tunnel import TlsOverTunnel

logger = get_logger("dns")

_DNS_QUERY_TIMEOUT = 5.0       # DoT/DoH 单次查询超时（pipeline 内部 wait_for）
_MAX_RESPONSE_BYTES = 64 * 1024


# ============================================================
# 抽象接口
# ============================================================

class Upstream:
    """所有 upstream 共用接口。"""

    async def query(self, data: bytes) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        return


# ============================================================
# UdpUpstream（保持现有 _DnsTunnel 行为；UDP-over-tunnel = TCP-relayed-DNS）
# ============================================================

class UdpUpstream(Upstream):
    """
    DNS-over-TCP via pyrealiy tunnel。**实际上服务端再发出去仍是 plain TCP/53**，
    所以这是 plaintext DNS（依赖 pyrealiy 隧道做线缆加密，不防上游运营商）。

    内部维持长寿命隧道 + tx_id rewriting + 并发 pipeline。沿用之前 _DnsTunnel 的设计。
    """

    MAX_INFLIGHT = 64

    def __init__(self, outbound, host: str, port: int = 53):
        self._outbound = outbound
        self._host = host
        self._port = port
        self._ready = None
        self._reader_task: Optional[asyncio.Task] = None
        self._setup_lock = asyncio.Lock()
        self._inflight = asyncio.Semaphore(self.MAX_INFLIGHT)
        self._pending: dict = {}
        self._next_tx = 1

    async def query(self, data: bytes) -> bytes:
        if len(data) < 2:
            raise ValueError("DNS query too short")
        async with self._inflight:
            for attempt in (0, 1):
                try:
                    await self._ensure_ready()
                    return await self._send_recv(data)
                except asyncio.TimeoutError:
                    raise
                except (Exception, asyncio.CancelledError):
                    self._drop_tunnel()
                    if attempt == 1:
                        raise
        # unreachable
        raise OSError("DNS query failed")

    async def _ensure_ready(self) -> None:
        if self._ready is not None and self._reader_task and not self._reader_task.done():
            return
        async with self._setup_lock:
            if self._ready is not None and self._reader_task and not self._reader_task.done():
                return
            ready = await self._outbound.acquire_tunnel()
            if ready is None:
                raise OSError(f"no tunnel available for DNS via '{self._outbound.tag}'")
            try:
                await ready.tunnel.send(pack_address(self._host, self._port))
            except Exception:
                try:
                    ready.close()
                except Exception:
                    pass
                raise
            self._ready = ready
            self._reader_task = asyncio.create_task(self._reader_loop())

    def _allocate_tx(self) -> int:
        for _ in range(0x10000):
            tx_id = self._next_tx
            self._next_tx = (self._next_tx + 1) & 0xFFFF
            if self._next_tx == 0:
                self._next_tx = 1
            if tx_id not in self._pending:
                return tx_id
        raise OSError("DNS pipeline tx_id space exhausted")

    async def _send_recv(self, data: bytes) -> bytes:
        original_id = data[:2]
        tx_id = self._allocate_tx()
        rewritten = tx_id.to_bytes(2, "big") + data[2:]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[tx_id] = (fut, original_id)
        try:
            await self._ready.tunnel.send(struct.pack("!H", len(rewritten)) + rewritten)
            resp = await asyncio.wait_for(fut, timeout=_DNS_QUERY_TIMEOUT)
            return original_id + resp[2:]
        finally:
            self._pending.pop(tx_id, None)

    async def _reader_loop(self) -> None:
        buf = bytearray()
        try:
            while True:
                chunk = await self._ready.tunnel.recv()
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 2:
                    length = int.from_bytes(buf[:2], "big")
                    if len(buf) < 2 + length:
                        break
                    resp = bytes(buf[2:2 + length])
                    del buf[:2 + length]
                    if len(resp) >= 2:
                        tx_id = int.from_bytes(resp[:2], "big")
                        pair = self._pending.pop(tx_id, None)
                        if pair is not None:
                            fut, _orig = pair
                            if not fut.done():
                                fut.set_result(resp)
        except Exception as e:
            for fut, _orig in self._pending.values():
                if not fut.done():
                    fut.set_exception(OSError(f"DNS tunnel reader ended: {e}"))
            self._pending.clear()
        finally:
            self._drop_tunnel()

    def _drop_tunnel(self) -> None:
        if self._ready is not None:
            try:
                self._ready.close()
            except Exception:
                pass
            self._ready = None
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None

    def close(self) -> None:
        self._drop_tunnel()


# ============================================================
# DotUpstream（DNS-over-TLS）
# ============================================================

class DotUpstream(Upstream):
    """
    DoT：pyrealiy 隧道 → server 端 plain TCP 到 host:853 → 本地 TLS 客户端做端到端
    TLS 1.2+ 握手 → 长度前缀 DNS。

    pipeline 同 UdpUpstream：tx_id rewriting + 并发 inflight。
    """

    MAX_INFLIGHT = 32

    def __init__(self, outbound, host: str, port: int = 853, sni: Optional[str] = None):
        self._outbound = outbound
        self._host = host
        self._port = port
        self._sni = sni or host
        self._ready = None
        self._tls: Optional[TlsOverTunnel] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._setup_lock = asyncio.Lock()
        self._inflight = asyncio.Semaphore(self.MAX_INFLIGHT)
        self._pending: dict = {}
        self._next_tx = 1

    async def query(self, data: bytes) -> bytes:
        if len(data) < 2:
            raise ValueError("DNS query too short")
        async with self._inflight:
            for attempt in (0, 1):
                try:
                    await self._ensure_ready()
                    return await self._send_recv(data)
                except asyncio.TimeoutError:
                    raise
                except (Exception, asyncio.CancelledError):
                    self._drop()
                    if attempt == 1:
                        raise
        raise OSError("DoT query failed")

    async def _ensure_ready(self) -> None:
        if self._tls is not None and self._reader_task and not self._reader_task.done():
            return
        async with self._setup_lock:
            if self._tls is not None and self._reader_task and not self._reader_task.done():
                return
            ready = await self._outbound.acquire_tunnel()
            if ready is None:
                raise OSError(f"no tunnel for DoT via '{self._outbound.tag}'")
            try:
                await ready.tunnel.send(pack_address(self._host, self._port))
                tls = TlsOverTunnel(ready.tunnel, server_hostname=self._sni)
                await asyncio.wait_for(tls.do_handshake(), timeout=10.0)
            except Exception:
                try:
                    ready.close()
                except Exception:
                    pass
                raise
            self._ready = ready
            self._tls = tls
            self._reader_task = asyncio.create_task(self._reader_loop())
            logger.debug("DoT %s:%d TLS handshake ok via %s", self._host, self._port, self._outbound.tag)

    def _allocate_tx(self) -> int:
        for _ in range(0x10000):
            tx_id = self._next_tx
            self._next_tx = (self._next_tx + 1) & 0xFFFF
            if self._next_tx == 0:
                self._next_tx = 1
            if tx_id not in self._pending:
                return tx_id
        raise OSError("DoT pipeline tx_id space exhausted")

    async def _send_recv(self, data: bytes) -> bytes:
        original_id = data[:2]
        tx_id = self._allocate_tx()
        rewritten = tx_id.to_bytes(2, "big") + data[2:]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[tx_id] = (fut, original_id)
        try:
            await self._tls.send(struct.pack("!H", len(rewritten)) + rewritten)
            resp = await asyncio.wait_for(fut, timeout=_DNS_QUERY_TIMEOUT)
            return original_id + resp[2:]
        finally:
            self._pending.pop(tx_id, None)

    async def _reader_loop(self) -> None:
        buf = bytearray()
        try:
            while True:
                chunk = await self._tls.recv()
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 2:
                    length = int.from_bytes(buf[:2], "big")
                    if length > _MAX_RESPONSE_BYTES:
                        raise OSError(f"DoT response too large: {length}")
                    if len(buf) < 2 + length:
                        break
                    resp = bytes(buf[2:2 + length])
                    del buf[:2 + length]
                    if len(resp) >= 2:
                        tx_id = int.from_bytes(resp[:2], "big")
                        pair = self._pending.pop(tx_id, None)
                        if pair is not None:
                            fut, _orig = pair
                            if not fut.done():
                                fut.set_result(resp)
        except Exception as e:
            for fut, _orig in self._pending.values():
                if not fut.done():
                    fut.set_exception(OSError(f"DoT reader ended: {e}"))
            self._pending.clear()
        finally:
            self._drop()

    def _drop(self) -> None:
        if self._ready is not None:
            try:
                self._ready.close()
            except Exception:
                pass
            self._ready = None
        self._tls = None
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None

    def close(self) -> None:
        self._drop()


# ============================================================
# DohUpstream（DNS-over-HTTPS）
# ============================================================

_RE_CONTENT_LENGTH = re.compile(rb"\r\nContent-Length:\s*(\d+)\r\n", re.IGNORECASE)


class DohUpstream(Upstream):
    """
    DoH：pyrealiy 隧道 → server 端 plain TCP 到 host:443 → 本地 TLS + HTTP/1.1
    POST /path application/dns-message。

    串行化：单 TLS 连接上一次只跑一个 HTTP transaction（HTTP/1.1 不 multiplex；
    HTTP/2 留待将来）。`asyncio.Lock` 保护写入路径。

    限制：URL 的 host 必须是 IP literal。否则 chicken-and-egg。
    """

    def __init__(self, outbound, host_ip: str, path: str, port: int = 443,
                 host_header: Optional[str] = None):
        self._outbound = outbound
        self._host_ip = host_ip
        self._path = path or "/dns-query"
        self._port = port
        self._host_header = host_header or host_ip
        self._ready = None
        self._tls: Optional[TlsOverTunnel] = None
        self._setup_lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()

    async def query(self, data: bytes) -> bytes:
        for attempt in (0, 1):
            try:
                await self._ensure_ready()
                async with self._req_lock:
                    return await asyncio.wait_for(self._do_request(data), timeout=_DNS_QUERY_TIMEOUT)
            except (Exception, asyncio.CancelledError):
                self._drop()
                if attempt == 1:
                    raise
        raise OSError("DoH query failed")

    async def _ensure_ready(self) -> None:
        if self._tls is not None:
            return
        async with self._setup_lock:
            if self._tls is not None:
                return
            ready = await self._outbound.acquire_tunnel()
            if ready is None:
                raise OSError(f"no tunnel for DoH via '{self._outbound.tag}'")
            try:
                await ready.tunnel.send(pack_address(self._host_ip, self._port))
                tls = TlsOverTunnel(ready.tunnel, server_hostname=self._host_header,
                                    alpn=["http/1.1"])
                await asyncio.wait_for(tls.do_handshake(), timeout=10.0)
            except Exception:
                try:
                    ready.close()
                except Exception:
                    pass
                raise
            self._ready = ready
            self._tls = tls
            logger.debug("DoH %s:%d TLS handshake ok via %s", self._host_ip, self._port, self._outbound.tag)

    async def _do_request(self, dns_msg: bytes) -> bytes:
        # 同步原始 tx_id —— DoH 协议本身没有 tx_id 概念，但 DNS 查询带的 tx_id 必须
        # 与响应一致。响应消息体本身就是 DNS wire format，里头 tx_id 应该等于请求的
        # （上游服务返回时复制 ID）。Cloudflare/Quad9 都遵守这个，不用重写。
        request = (
            f"POST {self._path} HTTP/1.1\r\n"
            f"Host: {self._host_header}\r\n"
            f"Accept: application/dns-message\r\n"
            f"Content-Type: application/dns-message\r\n"
            f"Content-Length: {len(dns_msg)}\r\n"
            f"Connection: keep-alive\r\n"
            f"\r\n"
        ).encode("ascii") + dns_msg

        await self._tls.send(request)

        # 读响应：先吞 header（直到 \r\n\r\n），再按 Content-Length 读 body
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = await self._tls.recv()
            buf.extend(chunk)
            if len(buf) > 8192:
                raise OSError("DoH response header too large")

        header_end = buf.index(b"\r\n\r\n")
        header_block = bytes(buf[:header_end])
        status_line = header_block.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if " 200 " not in status_line:
            raise OSError(f"DoH HTTP error: {status_line}")

        m = _RE_CONTENT_LENGTH.search(b"\r\n" + header_block + b"\r\n")
        if not m:
            raise OSError("DoH response lacks Content-Length")
        body_len = int(m.group(1))
        if body_len > _MAX_RESPONSE_BYTES:
            raise OSError(f"DoH body too large: {body_len}")

        body = buf[header_end + 4:]
        while len(body) < body_len:
            chunk = await self._tls.recv()
            body.extend(chunk)
        return bytes(body[:body_len])

    def _drop(self) -> None:
        if self._ready is not None:
            try:
                self._ready.close()
            except Exception:
                pass
            self._ready = None
        self._tls = None

    def close(self) -> None:
        self._drop()


# ============================================================
# 工厂
# ============================================================

def make_upstream(outbound, address: str) -> Upstream:
    """
    根据 address scheme 选 upstream 类型。

    "1.1.1.1:53"  → UdpUpstream
    "tls://1.1.1.1:853"  → DotUpstream
    "https://1.1.1.1/dns-query"  → DohUpstream
    """
    addr = (address or "").strip()
    if not addr:
        raise ValueError("DNS upstream address is empty")

    low = addr.lower()
    if low.startswith("https://"):
        u = urlparse(addr)
        if not u.hostname:
            raise ValueError(f"DoH URL missing host: {address}")
        if not _is_ip_literal(u.hostname):
            raise ValueError(
                f"DoH URL host must be an IP literal (got {u.hostname!r}); "
                f"domain hosts cause bootstrap chicken-and-egg")
        port = u.port or 443
        return DohUpstream(outbound, u.hostname, u.path or "/dns-query", port,
                           host_header=u.hostname)

    if low.startswith("tls://"):
        rest = addr[6:]
        host, port = _split_host_port(rest, default_port=853)
        return DotUpstream(outbound, host, port)

    if low.startswith("dns://"):
        rest = addr[6:]
        host, port = _split_host_port(rest, default_port=53)
        return UdpUpstream(outbound, host, port)

    # bare "host:port" or "host"
    host, port = _split_host_port(addr, default_port=53)
    return UdpUpstream(outbound, host, port)


def _split_host_port(s: str, default_port: int) -> tuple[str, int]:
    if s.startswith("["):
        # [::1]:port
        end = s.index("]")
        host = s[1:end]
        rest = s[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        return host, int(port_s)
    return s, default_port


def _is_ip_literal(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
