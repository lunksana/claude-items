"""
路由决策 + DNS 响应缓存。

两个缓存共享一个 LRU，按域名 key 索引：
  RoutingCache  domain → outbound_tag        (TCP dispatch 命中后跳过 router.match)
  DnsCache      (domain, qtype) → bytes      (DNS 转发命中后跳过上游查询)

DNS 缓存的 TTL 直接从响应的 answer 段里取 min(TTL)；路由缓存的 TTL 固定（默认 1h，
因为路由规则相对静态）。两个缓存都内存上限 10k entries（LRU），过期 entry 在 get
时延迟回收。

集成点：
  client.py _dispatch   → 命中跳过 router.match
  dns_forwarder         → 命中直接回包，不查上游
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

# 默认上限
_DEFAULT_MAX_ENTRIES = 10000
_ROUTING_TTL_SEC = 3600.0          # 路由规则相对静态，1h 足够
_DNS_TTL_MIN_SEC = 30.0            # 最小 DNS 缓存（防 TTL=0 / 5s 的滥发）
_DNS_TTL_MAX_SEC = 3600.0          # 最大 DNS 缓存（防 TTL 撒大谎）
_DNS_NEGATIVE_TTL = 60.0           # 无 answer 的响应（NXDOMAIN / SERVFAIL）


# ============================================================
# RoutingCache：domain → outbound_tag
# ============================================================

class RoutingCache:
    """LRU + TTL. 全部操作在 asyncio 单线程，无锁。"""

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES,
                 ttl_sec: float = _ROUTING_TTL_SEC):
        self._max = max_entries
        self._ttl = ttl_sec
        # OrderedDict 用 LRU：put 移到末尾，evict 弹首
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, domain: str) -> Optional[str]:
        key = domain.lower()
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        tag, expires = entry
        if time.monotonic() >= expires:
            del self._items[key]
            self.misses += 1
            return None
        # LRU: 重排到末尾
        self._items.move_to_end(key)
        self.hits += 1
        return tag

    def put(self, domain: str, outbound_tag: str) -> None:
        key = domain.lower()
        expires = time.monotonic() + self._ttl
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = (outbound_tag, expires)
        # evict
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def invalidate(self, domain: Optional[str] = None) -> int:
        """单条 / 全清。返回被清条数。"""
        if domain is None:
            n = len(self._items)
            self._items.clear()
            return n
        return 1 if self._items.pop(domain.lower(), None) else 0

    def stats(self) -> dict:
        return {
            "entries": len(self._items),
            "max":     self._max,
            "hits":    self.hits,
            "misses":  self.misses,
            "ttl_sec": self._ttl,
        }


# ============================================================
# DnsCache：(domain, qtype) → raw response bytes
# ============================================================

class DnsCache:
    """LRU + TTL（TTL 从 DNS 响应里抽 min(answer.TTL)）。"""

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES):
        self._max = max_entries
        self._items: OrderedDict[tuple[str, int], tuple[bytes, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, domain: str, qtype: int) -> Optional[bytes]:
        key = (domain.lower(), qtype)
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        resp, expires = entry
        if time.monotonic() >= expires:
            del self._items[key]
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return resp

    def put(self, domain: str, qtype: int, response: bytes) -> None:
        ttl = _extract_min_ttl(response)
        if ttl <= 0:
            return  # 别缓存（响应坏了）
        key = (domain.lower(), qtype)
        expires = time.monotonic() + ttl
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = (response, expires)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def invalidate(self) -> int:
        """全清。返回被清条数。给热加载调（路由规则变了，老 IP 可能错配出口）。"""
        n = len(self._items)
        self._items.clear()
        return n

    def stats(self) -> dict:
        return {
            "entries": len(self._items),
            "max":     self._max,
            "hits":    self.hits,
            "misses":  self.misses,
        }


# ============================================================
# DNS 报文解析：取 question 的 qtype + answer 段 min TTL
# ============================================================

def extract_qtype(query: bytes) -> Optional[int]:
    """从 DNS query 报文取 first question 的 qtype (1=A, 28=AAAA, ...)。"""
    if len(query) < 12:
        return None
    qdcount = int.from_bytes(query[4:6], "big")
    if qdcount < 1:
        return None
    # 跳过 QNAME
    offset = 12
    while offset < len(query):
        n = query[offset]
        if n == 0:
            offset += 1
            break
        if n & 0xC0:                    # pointer 不该出现在 question
            return None
        offset += n + 1
    if offset + 4 > len(query):
        return None
    return int.from_bytes(query[offset:offset + 2], "big")


def _extract_min_ttl(resp: bytes) -> float:
    """
    从 DNS 响应 answer 段取 min TTL，截断到 [_DNS_TTL_MIN_SEC, _DNS_TTL_MAX_SEC]。
    无 answer（NXDOMAIN / SERVFAIL）→ 返回 _DNS_NEGATIVE_TTL。
    报文损坏 → 返回 0（调用方丢弃不缓存）。
    """
    if len(resp) < 12:
        return 0.0
    qdcount = int.from_bytes(resp[4:6], "big")
    ancount = int.from_bytes(resp[6:8], "big")

    # 跳过 question 段
    offset = 12
    for _ in range(qdcount):
        while offset < len(resp):
            n = resp[offset]
            offset += 1
            if n == 0:
                break
            if n & 0xC0:
                offset += 1
                break
            offset += n
        offset += 4   # QTYPE + QCLASS
        if offset > len(resp):
            return 0.0

    if ancount == 0:
        return _DNS_NEGATIVE_TTL

    # 扫 answer
    min_ttl = float("inf")
    for _ in range(ancount):
        if offset >= len(resp):
            return 0.0
        # NAME
        if resp[offset] & 0xC0:
            offset += 2
        else:
            while offset < len(resp) and resp[offset] != 0:
                m = resp[offset]
                if m & 0xC0:
                    offset += 2
                    break
                offset += m + 1
            else:
                if offset < len(resp):
                    offset += 1
        # TYPE(2) + CLASS(2) + TTL(4) + RDLENGTH(2)
        if offset + 10 > len(resp):
            return 0.0
        ttl = int.from_bytes(resp[offset + 4:offset + 8], "big")
        rdlength = int.from_bytes(resp[offset + 8:offset + 10], "big")
        offset += 10 + rdlength
        if ttl < min_ttl:
            min_ttl = ttl

    if min_ttl == float("inf"):
        return _DNS_NEGATIVE_TTL
    return max(_DNS_TTL_MIN_SEC, min(_DNS_TTL_MAX_SEC, float(min_ttl)))
