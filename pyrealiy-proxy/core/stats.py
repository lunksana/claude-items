"""Server-side connection stats and IP block list."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


@dataclass
class ConnInfo:
    id: int
    client_ip: str
    target_host: str
    target_port: int
    started: float = field(default_factory=time.monotonic)
    bytes_up: int = 0    # client → target
    bytes_down: int = 0  # target → client
    writer: object = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started

    def as_dict(self, closed_at: float | None = None) -> dict:
        d = {
            "id":         self.id,
            "client_ip":  self.client_ip,
            "target":     f"{self.target_host}:{self.target_port}",
            "duration":   round(self.duration, 1),
            "bytes_up":   self.bytes_up,
            "bytes_down": self.bytes_down,
        }
        if closed_at is not None:
            d["closed_at"] = closed_at
        return d


class StatsStore:
    # domain_conns/domain_bytes 用 LRU OrderedDict 防止长跑 OOM
    # 5000 域名 ≈ 250KB，足够覆盖绝大多数活跃站点
    MAX_DOMAINS = 5000

    def __init__(self) -> None:
        self._counter    = 0
        self.active:       dict[int, ConnInfo]         = {}
        self.recent:       deque[dict]                 = deque(maxlen=50)
        self.domain_conns: OrderedDict[str, int]       = OrderedDict()
        self.domain_bytes: OrderedDict[str, int]       = OrderedDict()
        self.blocked:       set[str]                    = set()
        self.total_conns    = 0
        self.total_bytes_up = 0    # cumulative from closed connections
        self.total_bytes_dn = 0

    # ── LRU 维护 ───────────────────────────────────────────────────────────────

    def _touch_conn(self, host: str) -> None:
        if host in self.domain_conns:
            self.domain_conns[host] += 1
            self.domain_conns.move_to_end(host)
        else:
            if len(self.domain_conns) >= self.MAX_DOMAINS:
                self.domain_conns.popitem(last=False)  # 淘汰最久未访问
            self.domain_conns[host] = 1

    def _touch_bytes(self, host: str, delta: int) -> None:
        if delta <= 0:
            return
        if host in self.domain_bytes:
            self.domain_bytes[host] += delta
            self.domain_bytes.move_to_end(host)
        else:
            if len(self.domain_bytes) >= self.MAX_DOMAINS:
                self.domain_bytes.popitem(last=False)
            self.domain_bytes[host] = delta

    # ── connection lifecycle ───────────────────────────────────────────────────

    def register(self, client_ip: str, target_host: str, target_port: int,
                 writer=None) -> ConnInfo:
        self._counter += 1
        self.total_conns += 1
        conn = ConnInfo(self._counter, client_ip, target_host, target_port,
                        writer=writer)
        self.active[conn.id] = conn
        self._touch_conn(target_host)
        return conn

    def unregister(self, conn: ConnInfo) -> None:
        self.active.pop(conn.id, None)
        self._touch_bytes(conn.target_host, conn.bytes_up + conn.bytes_down)
        self.total_bytes_up += conn.bytes_up
        self.total_bytes_dn += conn.bytes_down
        self.recent.appendleft(conn.as_dict(closed_at=time.time()))

    # ── block list ─────────────────────────────────────────────────────────────

    def is_blocked(self, ip: str) -> bool:
        return ip in self.blocked

    def block(self, ip: str) -> None:
        self.blocked.add(ip)
        for conn in list(self.active.values()):
            if conn.client_ip == ip:
                _close_writer(conn.writer)

    def unblock(self, ip: str) -> None:
        self.blocked.discard(ip)

    def kill(self, conn_id: int) -> bool:
        conn = self.active.get(conn_id)
        if conn:
            _close_writer(conn.writer)
            return True
        return False

    # ── snapshot for admin API ─────────────────────────────────────────────────

    def snapshot(self) -> dict:
        conns = [c.as_dict() for c in sorted(self.active.values(),
                                              key=lambda c: c.started)]

        # top_domains 由前端按多种维度排序展示（默认按 bytes）。
        # 服务端必须把"按 conns top N"和"按 bytes top N"两个维度的并集都送出去，
        # 否则前端切换排序维度时，会看到流量大但连接少的域名被裁掉（口径不一致 bug）
        TOP_N = 50
        all_domains = [
            {"domain": d, "conns": self.domain_conns[d],
             "bytes":  self.domain_bytes.get(d, 0)}
            for d in self.domain_conns
        ]
        by_conns = sorted(all_domains, key=lambda x: x["conns"], reverse=True)[:TOP_N]
        by_bytes = sorted(all_domains, key=lambda x: x["bytes"], reverse=True)[:TOP_N]
        seen: set[str] = set()
        top_domains: list[dict] = []
        for d in by_conns + by_bytes:
            if d["domain"] not in seen:
                seen.add(d["domain"])
                top_domains.append(d)

        # include bytes from currently active connections
        act_up = sum(c.bytes_up for c in self.active.values())
        act_dn = sum(c.bytes_down for c in self.active.values())
        return {
            "total_conns":   self.total_conns,
            "active_count":  len(self.active),
            "total_bytes_up": self.total_bytes_up + act_up,
            "total_bytes_dn": self.total_bytes_dn + act_dn,
            "blocked":       sorted(self.blocked),
            "connections":   conns,
            "recent":        list(self.recent),
            "top_domains":   top_domains,
        }


def _close_writer(writer) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        pass
