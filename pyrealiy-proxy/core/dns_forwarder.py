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


def _question_end(data: bytes) -> int:
    """返回 question section 结束后的字节偏移；解析失败时回退到长度上限"""
    try:
        qdcount = int.from_bytes(data[4:6], "big")
        pos = 12
        for _ in range(qdcount):
            # QNAME：labels 直到 0x00（question 段不允许指针压缩）
            while pos < len(data):
                n = data[pos]
                if n == 0:
                    pos += 1
                    break
                if n & 0xC0:           # 防御性：偶遇指针就跳两字节
                    pos += 2
                    break
                pos += 1 + n
            pos += 4  # QTYPE(2) + QCLASS(2)
            if pos > len(data):
                return len(data)
        return pos
    except Exception:
        return len(data)


def _nxdomain(query: bytes) -> bytes:
    """保留原始 ID 与 question 段，置 QR=1 RCODE=3，清空 answer/authority/additional。

    必须只保留 question 段；如果带尾部 EDNS0/OPT 等 additional record，
    报头宣称 ARCOUNT=0 但附带数据会让严格 resolver 判 malformed。
    """
    if len(query) < 12:
        return query
    end = _question_end(query)
    h = bytearray(query[:12])
    h[2] = h[2] | 0x80                # QR=1，保留 Opcode/AA/TC/RD 等其他位
    h[3] = (h[3] & 0xF0) | 0x03       # RCODE=NXDOMAIN
    h[6] = h[7] = h[8] = h[9] = h[10] = h[11] = 0
    return bytes(h) + query[12:end]


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


class _DnsTunnel:
    """
    长寿命 DNS-over-TCP 隧道。

    问题：原实现每次 DNS 查询都 acquire 一条池连接，握手一遍，发完一帧丢弃。
    Chrome 单页加载触发 20+ DNS 查询时，池连接会被反复借走重建，每条
    重建耗时 ~200ms（TLS 1.3 伪装握手），严重挤占代理带宽。

    方案：从池中借出 **一条** 隧道并永久持有，所有 DNS 查询复用它。
    多查询用 asyncio.Lock 串行化——DNS 单查询 < 50ms，串行化比重建握手
    便宜得多；如果隧道掉线则惰性重建。

    与 BrutalPool 的关系：占用 1 个池槽位（池会自动补满），不另开连接。
    """

    def __init__(self, pool, remote_dns: str):
        self._pool       = pool
        self._remote_dns = remote_dns
        self._ready      = None
        self._lock       = asyncio.Lock()  # 串行化 query

    async def query(self, data: bytes) -> bytes:
        async with self._lock:
            # 一次重试机会：第一次失败时丢弃旧隧道、换新的再试
            for attempt in (0, 1):
                try:
                    await self._ensure_ready()
                    return await self._send_recv(data)
                except Exception:
                    self._drop_tunnel()
                    if attempt == 1:
                        raise

    async def _ensure_ready(self) -> None:
        if self._ready is not None:
            return
        ready = await self._pool.acquire()
        if ready is None:
            raise OSError("no tunnel available for DNS")
        # 只在首次发送目标地址，后续所有 DNS 查询复用同一目标连接
        await ready.tunnel.send(pack_address(self._remote_dns, 53))
        self._ready = ready

    async def _send_recv(self, data: bytes) -> bytes:
        await self._ready.tunnel.send(struct.pack("!H", len(data)) + data)
        buf = bytearray()
        while True:
            chunk = await asyncio.wait_for(self._ready.tunnel.recv(), timeout=_TUNNEL_TIMEOUT)
            buf.extend(chunk)
            if len(buf) >= 2:
                resp_len = struct.unpack("!H", bytes(buf[:2]))[0]
                if len(buf) >= 2 + resp_len:
                    return bytes(buf[2: 2 + resp_len])

    def _drop_tunnel(self) -> None:
        if self._ready is not None:
            try:
                self._ready.close()
            except Exception:
                pass
            self._ready = None

    def close(self) -> None:
        self._drop_tunnel()


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
        self._dns_tunnel = _DnsTunnel(pool, self._remote_dns)

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DNSProtocol(self._handle),
            local_addr=(self._host, self._port),
        )
        logger.info(
            "DNS forwarder on %s:%d  (CN->%s  foreign->%s via tunnel)",
            self._host, self._port, self._cn_dns, self._remote_dns,
        )

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
        self._dns_tunnel.close()

    async def _handle(self, data: bytes) -> bytes | None:
        domain = _extract_domain(data)
        if domain is None:
            return None

        action, source = self._router.match(domain)
        try:
            if action == REJECT:
                logger.debug("DNS REJECT  %s  [%s]", domain, source)
                return _nxdomain(data)
            if action == DIRECT:
                logger.debug("DNS DIRECT  %s -> %s  [%s]", domain, self._cn_dns, source)
                return await _udp_query(data, self._cn_dns)
            logger.debug("DNS PROXY   %s -> %s (tunnel)  [%s]", domain, self._remote_dns, source)
            return await self._dns_tunnel.query(data)
        except Exception as e:
            logger.warning("DNS query failed for %s: %s", domain, e)
            return _nxdomain(data)
