"""
DNS 转发器

本地监听 UDP 53/5353，按路由规则分流：
  DIRECT → UDP 直接查询国内 DNS（如 223.5.5.5）
  PROXY  → DNS-over-TCP 通过隧道查询境外 DNS（如 8.8.8.8）
  REJECT → 返回 NXDOMAIN

使用方法：将系统 DNS 改为 127.0.0.1:<port>，或用 iptables 将 53 端口重定向到该端口。
"""

from __future__ import annotations

import asyncio
import struct

from .router import DIRECT, REJECT
from .utils import get_logger, pack_address

logger = get_logger("dns")

_CN_DNS_DEFAULT     = "223.5.5.5"
_REMOTE_DNS_DEFAULT = "8.8.8.8"
_UDP_TIMEOUT        = 5.0
_TUNNEL_TIMEOUT     = 8.0


# ── DNS 报文工具 ───────────────────────────────────────────────────────────────

def _extract_domain(data: bytes) -> str | None:
    """从 DNS 查询报文中提取 QNAME（question 段不含指针压缩）"""
    try:
        offset = 12  # 跳过 12 字节固定头
        labels: list[str] = []
        while offset < len(data):
            n = data[offset]
            offset += 1
            if n == 0:
                break
            if n & 0xC0:  # 指针压缩不应出现在 question 段
                break
            labels.append(data[offset: offset + n].decode("ascii", errors="replace"))
            offset += n
        return ".".join(labels).lower() if labels else None
    except Exception:
        return None


def _nxdomain(query: bytes) -> bytes:
    """保留原始 ID，置 QR=1 RCODE=3，清空 answer/authority/additional"""
    if len(query) < 12:
        return query
    h = bytearray(query[:12])
    h[2] = (h[2] | 0x80) & 0xFE   # QR=1，清 RD（bit 0）
    h[3] = (h[3] & 0xF0) | 0x03   # RCODE=NXDOMAIN
    h[6] = h[7] = h[8] = h[9] = h[10] = h[11] = 0
    return bytes(h) + query[12:]


# ── 查询后端 ───────────────────────────────────────────────────────────────────

async def _udp_query(data: bytes, host: str, port: int = 53) -> bytes:
    """向国内 DNS 发送 UDP 查询"""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[bytes] = loop.create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, d, _):
            if not fut.done():
                fut.set_result(d)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

        def connection_lost(self, _):
            if not fut.done():
                fut.set_exception(OSError("connection lost"))

    transport, _ = await loop.create_datagram_endpoint(_Proto, remote_addr=(host, port))
    try:
        transport.sendto(data)
        return await asyncio.wait_for(fut, timeout=_UDP_TIMEOUT)
    finally:
        transport.close()


async def _tunnel_query(data: bytes, pool, remote_dns: str) -> bytes:
    """
    DNS-over-TCP 通过隧道。

    协议：
      1. tunnel.send(pack_address(remote_dns, 53))  → 服务端连接目标
      2. tunnel.send(2字节长度 + DNS查询)           → 发送 DNS-over-TCP 请求
      3. 累积 tunnel.recv() 直到读满响应
    """
    ready = await pool.acquire()
    if ready is None:
        raise OSError("no tunnel available")
    try:
        await ready.tunnel.send(pack_address(remote_dns, 53))
        await ready.tunnel.send(struct.pack("!H", len(data)) + data)

        buf = bytearray()
        while True:
            chunk = await asyncio.wait_for(ready.tunnel.recv(), timeout=_TUNNEL_TIMEOUT)
            buf.extend(chunk)
            if len(buf) >= 2:
                resp_len = struct.unpack("!H", bytes(buf[:2]))[0]
                if len(buf) >= 2 + resp_len:
                    return bytes(buf[2: 2 + resp_len])
    finally:
        ready.close()


# ── asyncio 协议层 ─────────────────────────────────────────────────────────────

class _DNSProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self._handler   = handler
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data: bytes, addr):
        asyncio.create_task(self._dispatch(data, addr))

    async def _dispatch(self, data: bytes, addr):
        response = await self._handler(data)
        if response is not None and self._transport:
            self._transport.sendto(response, addr)


# ── 公共接口 ───────────────────────────────────────────────────────────────────

class DNSForwarder:
    def __init__(self, cfg: dict, router, pool):
        self._host       = cfg.get("dns_listen_host", "127.0.0.1")
        self._port       = int(cfg.get("dns_listen_port", 5353))
        self._cn_dns     = cfg.get("cn_dns", _CN_DNS_DEFAULT)
        self._remote_dns = cfg.get("remote_dns", _REMOTE_DNS_DEFAULT)
        self._router     = router
        self._pool       = pool
        self._transport  = None

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DNSProtocol(self._handle),
            local_addr=(self._host, self._port),
        )
        logger.info(
            "DNS forwarder on %s:%d  (CN→%s  foreign→%s via tunnel)",
            self._host, self._port, self._cn_dns, self._remote_dns,
        )

    def stop(self) -> None:
        if self._transport:
            self._transport.close()

    async def _handle(self, data: bytes) -> bytes | None:
        domain = _extract_domain(data)
        if domain is None:
            return None

        action = self._router.match(domain)
        try:
            if action == REJECT:
                logger.debug("DNS REJECT  %s", domain)
                return _nxdomain(data)
            if action == DIRECT:
                logger.debug("DNS DIRECT  %s → %s", domain, self._cn_dns)
                return await _udp_query(data, self._cn_dns)
            logger.debug("DNS PROXY   %s → %s (tunnel)", domain, self._remote_dns)
            return await _tunnel_query(data, self._pool, self._remote_dns)
        except Exception as e:
            logger.warning("DNS query failed for %s: %s", domain, e)
            return _nxdomain(data)
