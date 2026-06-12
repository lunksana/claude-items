"""
DNS 转发器

本地监听 UDP 53/5353，按 router 决定的 outbound tag 分流：
  block leaf    → 返回 NXDOMAIN
  direct leaf   → UDP 直查国内 DNS（如 223.5.5.5）—— UDP 比 TCP 显著省一次握手
  mirage leaf → 经该 outbound 的隧道做 DNS-over-TCP（pipeline，一个 DoT 隧道服多查）

组节点（urltest/fallback）按 resolve_leaf() 选定的叶子分流。每个 mirage 叶子
独占一个 _DnsTunnel pipeline（懒建），不同 outbound 之间不共享。

使用方法：将系统 DNS 改为 127.0.0.1:<port>，或用 iptables 把 53 端口重定向过来。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .outbound import Outbound, MirageOutbound
from .utils import get_logger
from .dns import make_upstream
from .dns.upstream import Upstream

logger = get_logger("dns")

_CN_DNS_DEFAULT     = "223.5.5.5"
_REMOTE_DNS_DEFAULT = "8.8.8.8"
_UDP_TIMEOUT        = 5.0


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


# _DnsTunnel 已迁移到 core/dns/upstream.py（UdpUpstream / DotUpstream / DohUpstream）。
# 通过 make_upstream(outbound, address) 工厂选 scheme。


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
    def __init__(self, cfg: dict, router, outbounds: dict[str, Outbound],
                 routing_cache=None, dns_cache=None):
        self._host       = cfg.get("dns_listen_host", "127.0.0.1")
        self._port       = int(cfg.get("dns_listen_port", 5353))
        self._cn_dns     = cfg.get("cn_dns", _CN_DNS_DEFAULT)
        self._remote_dns = cfg.get("remote_dns", _REMOTE_DNS_DEFAULT)
        self._router     = router
        self._outbounds  = outbounds
        self._transport  = None
        # 每个 mirage 叶子独占一条上游 pipeline，懒建。
        # 实际类型由 remote_dns 的 scheme 决定（UDP / DoT / DoH，见 make_upstream）。
        self._tunnels: dict[str, Upstream] = {}
        # 决策缓存（路由 + DNS 响应），None 时不做缓存
        self._routing_cache = routing_cache
        self._dns_cache = dns_cache

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DNSProtocol(self._handle),
            local_addr=(self._host, self._port),
        )
        logger.info(
            "DNS forwarder on %s:%d  (direct->%s  proxy->%s via outbound's tunnel)",
            self._host, self._port, self._cn_dns, self._remote_dns,
        )

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
        for t in self._tunnels.values():
            t.close()
        self._tunnels.clear()

    def reload(self, new_cfg: dict) -> None:
        """
        热加载：换 cn_dns / remote_dns；drop 所有现有 upstream 让下一次查询时按新地址重建。
        本地监听 host/port 改了不重 bind（schema 文档已说明）。
        """
        new_cn = new_cfg.get("cn_dns", _CN_DNS_DEFAULT)
        new_remote = new_cfg.get("remote_dns", _REMOTE_DNS_DEFAULT)
        if new_cn != self._cn_dns or new_remote != self._remote_dns:
            logger.info("dns reload: cn_dns %s -> %s, remote %s -> %s",
                        self._cn_dns, new_cn, self._remote_dns, new_remote)
            self._cn_dns = new_cn
            self._remote_dns = new_remote
            for t in self._tunnels.values():
                t.close()
            self._tunnels.clear()

    def _tunnel_for(self, leaf: MirageOutbound) -> Upstream:
        t = self._tunnels.get(leaf.tag)
        if t is None:
            t = make_upstream(leaf, self._remote_dns)
            self._tunnels[leaf.tag] = t
        return t

    async def _handle(self, data: bytes) -> Optional[bytes]:
        domain = _extract_domain(data)
        if domain is None:
            return None

        # ── DNS 响应缓存：命中直接回，跳过路由 + 上游 ─────────────────────
        qtype = None
        if self._dns_cache is not None:
            from .decision_cache import extract_qtype
            qtype = extract_qtype(data)
            if qtype is not None:
                cached = self._dns_cache.get(domain, qtype)
                if cached is not None:
                    # 把缓存响应里的 tx_id 换成本次查询的
                    return data[:2] + cached[2:]

        # ── 路由决策（先看缓存，miss 再算）───────────────────────────────
        action = None
        source = ""
        if self._routing_cache is not None:
            cached_tag = self._routing_cache.get(domain)
            if cached_tag is not None:
                action = cached_tag
                source = "cached"
        if action is None:
            action, source = self._router.match(domain)
            if self._routing_cache is not None:
                self._routing_cache.put(domain, action)

        outbound = self._outbounds.get(action)
        if outbound is None:
            logger.warning("DNS router returned unknown outbound '%s' for %s [%s]",
                           action, domain, source)
            return _nxdomain(data)

        leaf = outbound.resolve_leaf()

        try:
            if leaf.type == "block":
                logger.debug("DNS block   %s  [%s]", domain, source)
                return _nxdomain(data)
            if leaf.type == "direct":
                logger.debug("DNS direct  %s -> %s  [%s]", domain, self._cn_dns, source)
                resp = await _udp_query(data, self._cn_dns)
            elif isinstance(leaf, MirageOutbound):
                logger.debug("DNS via %-12s %s -> %s  [%s]",
                             leaf.tag, domain, self._remote_dns, source)
                resp = await self._tunnel_for(leaf).query(data)
            else:
                # leaf 是组本身（所有 child 均 unhealthy）/未来未知类型 → NXDOMAIN
                logger.warning("DNS '%s' [%s]: outbound '%s' has no healthy leaf, NXDOMAIN",
                               domain, source, outbound.tag)
                return _nxdomain(data)
        except Exception as e:
            logger.warning("DNS query failed for %s via %s: %s", domain, leaf.tag, e)
            return _nxdomain(data)

        # 写缓存（成功的 answer 才缓存）
        if self._dns_cache is not None and qtype is not None and resp is not None:
            self._dns_cache.put(domain, qtype, resp)
        return resp
