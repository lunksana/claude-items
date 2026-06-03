"""
Outbound 抽象（sing-box 风格）

每个 outbound 是一个具名 (tag) 出口；type 决定行为：
  叶子节点：
    pyrealiy   —— 加密隧道（每个 outbound 独占一个 BrutalPool）
    direct     —— 系统直连
    block      —— 拒绝
  组节点（见 core/group.py）：
    urltest    —— 选 latency 最低的 child（带 tolerance 防抖）
    fallback   —— 按顺序选第一个 healthy 的 child

通用 API：
  warmup() / stop()
  resolve_leaf()         —— 组沿当前选择一路展开到叶子；叶子返回自身
  handle(rdr, wtr, ...)  —— 完整接管一条本地连接的生命周期
  latency_ms / is_healthy / latency_age_sec —— 健康指标，给 urltest/fallback 用

Health 指标来源：
  pyrealiy outbound 把 build_one 的耗时回调进自己的滚动样本窗口
  （median 抗抖动），无需独立探测网络。长时间无流量时由 healthcheck 模块
  主动触发一次 probe 兜底。

向后兼容入口：build_outbounds(cfg) 自动识别老 server_host 顶层字段配置。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import statistics
import time
from collections import deque
from typing import Optional

from .conn_pool import BrutalPool, _ReadyTunnel
from .utils import get_logger, pack_address, relay, safe_close, get_drain_threshold

logger = get_logger("outbound")

# 健康判定阈值
_UNHEALTHY_AFTER_FAILS = 3    # 连续 build 失败 >= 该值 → unhealthy
_LATENCY_WINDOW        = 10   # 延迟样本数（统计 median）


# ── 基类 ──────────────────────────────────────────────────────────────────────

class Outbound:
    """所有 outbound 的共同基类"""
    tag: str = ""
    type: str = ""

    async def warmup(self) -> None:
        return

    async def stop(self) -> None:
        return

    def resolve_leaf(self) -> "Outbound":
        """组节点向下展开返回当前选定的叶子；叶子返回自身"""
        return self

    @property
    def latency_ms(self) -> Optional[float]:
        return None

    @property
    def latency_age_sec(self) -> float:
        return float("inf")

    @property
    def is_healthy(self) -> bool:
        return True

    async def handle(self, local_reader, local_writer, target_host: str, target_port: int) -> None:
        raise NotImplementedError


# ── 叶子节点 ──────────────────────────────────────────────────────────────────

class DirectOutbound(Outbound):
    """系统直连，不走任何加密"""
    type = "direct"

    def __init__(self, tag: str = "direct"):
        self.tag = tag

    async def handle(self, local_reader, local_writer, target_host, target_port):
        try:
            target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
        except Exception as e:
            logger.error("[%s] connect %s:%d failed: %s", self.tag, target_host, target_port, e)
            await safe_close(local_writer)
            return
        await relay(local_reader, target_writer, target_reader, local_writer,
                    label=f"{self.tag} {target_host}:{target_port}")


class BlockOutbound(Outbound):
    """拒绝连接（关闭本地端）"""
    type = "block"

    def __init__(self, tag: str = "block"):
        self.tag = tag

    async def handle(self, local_reader, local_writer, target_host, target_port):
        await safe_close(local_writer)


class PyrealiyOutbound(Outbound):
    """
    加密隧道叶子。每个 outbound 独占一个 BrutalPool。

    BrutalPool 在每次成功 build 后通过 on_latency 回调把耗时塞进 _latency_samples，
    资源占用极小：deque(maxlen=10) + 一个 float。
    """
    type = "pyrealiy"

    def __init__(self, tag: str, node_cfg: dict):
        self.tag = tag
        self._pool = BrutalPool(
            node_cfg,
            on_latency=self._record_latency,
            on_failure=self._record_failure,
        )
        self._latency_samples: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._last_sample_time = 0.0
        self._consecutive_failures = 0

    async def warmup(self) -> None:
        await self._pool.warmup()

    async def stop(self) -> None:
        # 池本身没有显式 close 路径，tunnel 由 stale 检测自然回收
        return

    async def acquire_tunnel(self) -> Optional[_ReadyTunnel]:
        """供 DNS 模块复用一条预建隧道做 DoT pipeline"""
        return await self._pool.acquire()

    @property
    def pool(self) -> BrutalPool:
        """暴露给 geo 模块用：geosite_cache 期望 pool.acquire() 的形状"""
        return self._pool

    async def probe(self) -> Optional[float]:
        """主动触发一次 build 收集延迟样本。返回 ms 或 None。"""
        ok = await self._pool.probe_once()
        if not ok:
            return None
        return self.latency_ms

    async def handle(self, local_reader, local_writer, target_host, target_port):
        ready = await self._pool.acquire()
        if ready is None:
            logger.error("[%s] no tunnel for %s:%d", self.tag, target_host, target_port)
            await safe_close(local_writer)
            return
        tunnel = ready.tunnel
        server_writer = ready.writer
        try:
            await tunnel.send(pack_address(target_host, target_port))
        except Exception as e:
            logger.error("[%s] send target addr failed: %s", self.tag, e)
            await safe_close(server_writer)
            await safe_close(local_writer)
            return
        await _bidi_tunnel_relay(local_reader, local_writer, tunnel, server_writer,
                                 label=f"{self.tag} {target_host}:{target_port}")

    # ── 健康回调（由 BrutalPool 调）──────────────────────────────────────
    def _record_latency(self, ms: float) -> None:
        self._latency_samples.append(ms)
        self._last_sample_time = time.monotonic()
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1

    @property
    def latency_ms(self) -> Optional[float]:
        if not self._latency_samples:
            return None
        return statistics.median(self._latency_samples)

    @property
    def latency_age_sec(self) -> float:
        if self._last_sample_time == 0.0:
            return float("inf")
        return time.monotonic() - self._last_sample_time

    @property
    def is_healthy(self) -> bool:
        return self._consecutive_failures < _UNHEALTHY_AFTER_FAILS


# ── 共用 relay ────────────────────────────────────────────────────────────────

async def _bidi_tunnel_relay(local_reader, local_writer, tunnel, server_writer, label: str = ""):
    """
    本地 ↔ 加密隧道 双向中继。从 client.py _dispatch 原 PROXY 分支提取。

    主动关方先发 TLS close_notify，让关闭字节序列与真实 HTTPS 一致。

    异常静默由 logger("outbound") DEBUG 级别可见：对端 RST / Timeout / EOF
    这些都是正常断开信号，默认不打日志；开 DEBUG 后能看到 leg 终止时的
    异常类型 + 消息，方便排查"代理莫名其妙就断了"这类问题。
    """
    async def local_to_tunnel():
        try:
            while True:
                data = await local_reader.read(65536)
                if not data:
                    break
                await tunnel.send(data)
        except Exception as e:
            logger.debug("relay %s local→tunnel ended: %s: %s",
                         label or "?", type(e).__name__, e)
        finally:
            await tunnel.send_close_notify()
            await safe_close(server_writer)

    async def tunnel_to_local():
        try:
            while True:
                data = await tunnel.recv()
                local_writer.write(data)
                if local_writer.transport.get_write_buffer_size() > get_drain_threshold():
                    await local_writer.drain()
        except Exception as e:
            logger.debug("relay %s tunnel→local ended: %s: %s",
                         label or "?", type(e).__name__, e)
        finally:
            await safe_close(local_writer)

    task_a = asyncio.create_task(local_to_tunnel())
    task_b = asyncio.create_task(tunnel_to_local())
    try:
        await asyncio.wait([task_a, task_b], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (task_a, task_b):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass


# ── 工厂 ──────────────────────────────────────────────────────────────────────

def _resolve_host_inplace(node_cfg: dict, tag: str) -> None:
    """
    把 node_cfg['server_host'] 中的域名解析为 IP 并替换。

    每条 pool 隧道都对 server_host 做一次 open_connection；如果它是域名，
    asyncio 在线程池里发 getaddrinfo —— 这个 future 不可取消，pool 超时
    后没人接住它的 gaierror，asyncio 会抱怨 "Future exception was never
    retrieved"。启动一次性解析既消除噪音根源，又能在启动期快速暴露
    "客户端连不上服务端"。
    """
    raw = node_cfg["server_host"]
    host = raw.strip() if isinstance(raw, str) else raw
    if isinstance(raw, str) and host != raw:
        logger.warning("[%s] server %r had surrounding whitespace, stripped to %r", tag, raw, host)
    node_cfg["server_host"] = host

    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        v4 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET})
        v6 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET6})
    except socket.gaierror as e:
        logger.error("[%s] cannot resolve %s: %s — check DNS / hosts / VPN", tag, host, e)
        raise SystemExit(1)

    ips = v4 + v6
    if not ips:
        logger.error("[%s] no A/AAAA record for %s", tag, host)
        raise SystemExit(1)

    node_cfg["server_host"] = ips[0]
    if len(ips) > 1:
        logger.info("[%s] %s -> %d IPs, using %s (others: %s)", tag, host, len(ips), ips[0], ", ".join(ips[1:]))
    else:
        logger.info("[%s] %s -> %s", tag, host, ips[0])


def _normalize_pyrealiy_cfg(item: dict) -> dict:
    """
    把 sing-box 风格的 outbound 字段（server / server_port / sni）规范化为
    BrutalPool 期望的内部字段（server_host / server_port / camouflage_host）。
    """
    return {
        "server_host":      item.get("server") or item.get("server_host"),
        "server_port":      int(item.get("server_port", 443)),
        "password":         item.get("password", ""),
        "camouflage_host":  item.get("sni") or item.get("camouflage_host", "www.apple.com"),
        "brutal_rate_bps":  int(item.get("brutal_rate_bps", 0)),
        "brutal_pool_size": int(item.get("brutal_pool_size", 10)),
    }


def _synthesize_legacy_outbound(cfg: dict) -> list[dict]:
    """老配置（server_host 顶层）→ 单 pyrealiy outbound"""
    logger.info("Legacy config detected (no 'outbounds'), synthesizing single pyrealiy outbound 'proxy'")
    return [{
        "type": "pyrealiy", "tag": "proxy",
        "server":           cfg["server_host"],
        "server_port":      cfg["server_port"],
        "password":         cfg["password"],
        "sni":              cfg.get("camouflage_host", "www.apple.com"),
        "brutal_rate_bps":  cfg.get("brutal_rate_bps", 0),
        "brutal_pool_size": cfg.get("brutal_pool_size", 10),
    }]


def build_outbounds(cfg: dict) -> dict[str, Outbound]:
    """
    从 cfg 构建 {tag: Outbound} 字典。

    新格式：cfg["outbounds"] = [...]
    老格式：cfg["server_host"] 等顶层字段 → 合成 'proxy' 单节点

    自动补全 'direct' 与 'block'（若未显式定义）。
    组节点的拓扑通过 fixpoint 循环解析，循环引用会停留在 pending 集合
    被显式 raise（无需额外的环检测代码）。
    """
    from .group import UrlTestGroup, FallbackGroup

    raw_list = cfg.get("outbounds")
    if not raw_list:
        if "server_host" not in cfg:
            raise ValueError("config missing both 'outbounds' and 'server_host'")
        raw_list = _synthesize_legacy_outbound(cfg)

    out: dict[str, Outbound] = {}
    deferred: list[dict] = []

    # 第一遍：实例化所有叶子（pyrealiy / direct / block）
    for item in raw_list:
        if not isinstance(item, dict):
            raise ValueError(f"outbound entry must be object, got {type(item).__name__}")
        tag = item.get("tag")
        otype = item.get("type")
        if not tag or not otype:
            raise ValueError(f"outbound missing tag/type: {item}")
        if not isinstance(tag, str) or not isinstance(otype, str):
            raise ValueError(f"outbound tag/type must be string: {item}")
        if tag in out:
            raise ValueError(f"duplicate outbound tag: {tag}")

        if otype == "pyrealiy":
            node_cfg = _normalize_pyrealiy_cfg(item)
            if not node_cfg["server_host"]:
                raise ValueError(f"pyrealiy outbound '{tag}' missing 'server'")
            _resolve_host_inplace(node_cfg, tag)
            out[tag] = PyrealiyOutbound(tag, node_cfg)
        elif otype == "direct":
            out[tag] = DirectOutbound(tag)
        elif otype == "block":
            out[tag] = BlockOutbound(tag)
        elif otype in ("urltest", "fallback"):
            deferred.append(item)
        else:
            raise ValueError(f"unknown outbound type '{otype}' for tag '{tag}'")

    # 自动补全 direct / block（沿用 sing-box 约定）
    if "direct" not in out:
        out["direct"] = DirectOutbound("direct")
        logger.info("Auto-added implicit 'direct' outbound")
    if "block" not in out:
        out["block"] = BlockOutbound("block")
        logger.info("Auto-added implicit 'block' outbound")

    # 第二遍：fixpoint 解析组（支持组引用组）
    pending = list(deferred)
    while pending:
        progress = False
        next_round = []
        for item in pending:
            tag = item["tag"]
            child_tags = item.get("outbounds") or []
            if not isinstance(child_tags, list) or not child_tags:
                raise ValueError(f"group '{tag}' has empty or invalid 'outbounds'")
            unresolved = [ct for ct in child_tags if ct not in out]
            if unresolved:
                next_round.append(item)
                continue
            children = [out[ct] for ct in child_tags]
            otype = item["type"]
            if otype == "urltest":
                tolerance = int(item.get("tolerance", 50))
                out[tag] = UrlTestGroup(tag, children, tolerance_ms=tolerance)
            else:
                out[tag] = FallbackGroup(tag, children)
            logger.info("Group '%s' (%s) → %s", tag, otype, child_tags)
            progress = True
        if not progress:
            bad = [(p["tag"], p.get("outbounds")) for p in next_round]
            raise ValueError(f"group outbounds reference undefined or circular tags: {bad}")
        pending = next_round

    return out
