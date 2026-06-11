"""
ConnectionRegistry + TrafficMeter：API 的活跃连接表 + 全局流量计。

被三处用：
  - client.py _dispatch / _dispatch_udp：注册 / 注销 ConnInfo，传 byte 回调
  - core/api/clash_endpoints.py：GET /connections 直接 dump
  - core/api/ws_endpoints.py（P4）：WS /traffic 1Hz 推 (up_total, down_total)

设计：
  - asyncio 单线程，所有 add/remove/iter 都在 loop 内，无需 Lock
  - ConnInfo 字段尽量贴 Clash 格式（Yacd / metacubexd 解析时少改字段名）
  - 关闭后 5 秒内仍可见（让 UI 看到"刚关闭"，避免快闪流量丢失）
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class ConnMetadata:
    network: str = "tcp"             # tcp | udp
    type: str = "Socks5"             # Socks5 | TProxy | HTTP | ...
    source_ip: str = ""
    source_port: str = ""
    destination_ip: str = ""
    destination_port: str = ""
    host: str = ""                   # routing host（域名优先；纯 IP 时 = destination_ip）
    process_path: str = ""           # 进程路径，目前为空（后续 P5 / 平台支持）

    def to_clash(self) -> dict:
        return {
            "network":         self.network,
            "type":            self.type,
            "sourceIP":        self.source_ip,
            "sourcePort":      self.source_port,
            "destinationIP":   self.destination_ip,
            "destinationPort": self.destination_port,
            "host":            self.host,
            "dnsMode":         "normal",
            "processPath":     self.process_path,
        }


@dataclass
class ConnInfo:
    id: str
    metadata: ConnMetadata
    upload: int = 0
    download: int = 0
    start_epoch: float = field(default_factory=time.time)
    start_monotonic: float = field(default_factory=time.monotonic)
    chains: list[str] = field(default_factory=list)        # [leaf_tag, ..., entry_outbound_tag]，Clash 期望"叶在前"
    rule: str = "default"                                  # "DomainSuffix" / "GeoIP" / "Match" / "default"
    rule_payload: str = ""                                 # "google.com" / "cn" / ""
    closed_at_monotonic: float = 0.0                       # 关闭时间；0 = 仍开

    def to_clash(self) -> dict:
        return {
            "id":          self.id,
            "metadata":    self.metadata.to_clash(),
            "upload":      self.upload,
            "download":    self.download,
            "start":       _iso(self.start_epoch),
            "chains":      list(self.chains),
            "rule":        self.rule,
            "rulePayload": self.rule_payload,
        }


def _iso(epoch: float) -> str:
    """ISO 8601 UTC `2026-06-11T12:00:00.000Z`（Clash UI 期望此格式）"""
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{int((epoch % 1) * 1000):03d}Z"


# ============================================================
# TrafficMeter
# ============================================================

class TrafficMeter:
    """全局累计上下行字节。1Hz 速率由 ws_endpoints.py 自己采样。"""

    def __init__(self) -> None:
        self.up_total: int = 0
        self.down_total: int = 0

    def add_up(self, n: int) -> None:
        self.up_total += n

    def add_down(self, n: int) -> None:
        self.down_total += n


# ============================================================
# ConnectionRegistry
# ============================================================

# 关闭后保留时间（秒）；让 UI 看到刚断的连接
_LINGER_SEC = 5.0


class ConnectionRegistry:
    def __init__(self) -> None:
        self._open: dict[str, ConnInfo] = {}
        self._closed: list[ConnInfo] = []
        self._meter = TrafficMeter()

    @property
    def meter(self) -> TrafficMeter:
        return self._meter

    # ---- 注册 / 注销 ----

    def register(self, info: ConnInfo) -> None:
        self._open[info.id] = info

    def unregister(self, conn_id: str) -> None:
        info = self._open.pop(conn_id, None)
        if info is not None:
            info.closed_at_monotonic = time.monotonic()
            self._closed.append(info)
        self._gc_closed()

    # ---- 浏览 ----

    def list_active(self) -> list[ConnInfo]:
        self._gc_closed()
        return list(self._open.values()) + [c for c in self._closed]

    def _gc_closed(self) -> None:
        if not self._closed:
            return
        cutoff = time.monotonic() - _LINGER_SEC
        self._closed = [c for c in self._closed if c.closed_at_monotonic >= cutoff]

    # ---- 集成辅助：返回 (on_up, on_down) 回调 ----

    def make_callbacks(self, info: ConnInfo) -> tuple[Callable[[int], None], Callable[[int], None]]:
        """返回 byte 回调，同时更新 ConnInfo 和全局 meter。注册 info 由调用方负责。"""
        meter = self._meter

        def on_up(n: int) -> None:
            info.upload += n
            meter.up_total += n

        def on_down(n: int) -> None:
            info.download += n
            meter.down_total += n

        return on_up, on_down


# ============================================================
# 辅助
# ============================================================

def new_conn_id() -> str:
    return uuid.uuid4().hex


def split_rule(source: str) -> tuple[str, str]:
    """
    router 给 _dispatch 的 source 字符串拆成 (rule_type, rule_payload)。
    支持几种形式：
      "DomainSuffix:google.com"        → ("DomainSuffix", "google.com")
      "GEOSITE: cn"                    → ("GEOSITE", "cn")
      "default"                        → ("Match", "default")
      "csv-row:5 PROXY"                → ("CSV", "row:5 PROXY")  保留原信息
    """
    if not source:
        return "Match", ""
    if ":" in source:
        kind, _, payload = source.partition(":")
        return kind.strip(), payload.strip()
    if source.lower() in ("default", "final", "match"):
        return "Match", source
    return source, ""
