"""
PyReality 客户端

本地监听 SOCKS5 → 路由判断 → 按命中的 outbound 走（每个 outbound 自管连接池）

多节点 / 自适应选路（sing-box 风格）：
  config_client.json 顶层 "outbounds" 数组定义节点 + 组：
    type=pyrealiy  → 加密隧道叶子节点，独占一个 BrutalPool
    type=direct    → 系统直连
    type=block     → 拒绝
    type=urltest   → 组：选 median latency 最低的 child（带 tolerance 防抖）
    type=fallback  → 组：按顺序选第一个 healthy 的 child

  延迟数据：复用 BrutalPool 每次 build 的握手耗时（无额外探测）；长时间
  无流量时由 HealthCheck 主动 probe 兜底。

可选 TProxy 透明代理（需要 root + iptables TPROXY 规则，由 setup.py 生成）

老配置（顶层 server_host）：build_outbounds 自动合成单 pyrealiy outbound 'proxy'。
老 rules（CSV 字符串）：PROXY/DIRECT/REJECT 关键字自动映射到 proxy/direct/block 三个 tag。

用法：
  python client.py [config_client.json]
"""


from __future__ import annotations

import asyncio
import json
import resource
import sys

from core.socks5 import parse_socks5_request
from core.outbound import build_outbounds, PyrealiyOutbound, Outbound
from core.dns_forwarder import DNSForwarder
from core.sniffer import sniff_domain, PrefixedReader
from core.geosite_cache import ensure_all as geo_ensure_all
from core.router import build_router, PROXY, DIRECT, REJECT
from core.healthcheck import HealthCheck
from core.utils import (get_logger, safe_close, apply_log_levels,
                        install_stale_gaierror_handler, set_drain_threshold)
from core import brutal

logger = get_logger("client")


_ACCESS_LOG = False     # 由 main() 从 cfg 读出后设置


def _alog(level: str, fmt: str, *args) -> None:
    """每连接的 dispatch 日志：默认关，cfg['access_log']=true 时开"""
    if not _ACCESS_LOG:
        return
    getattr(logger, level)(fmt, *args)


async def _dispatch(
    local_reader,
    local_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    outbounds: dict[str, Outbound],
    router,
    routing_host: str | None = None,
) -> None:
    """
    路由判断 + 委派给命中的 outbound。SOCKS5 / TProxy 两个入口共用。

    routing_host: 用于路由匹配的域名（TProxy sniff 结果）；
                  None 时退化为 target_host。
    target_host:  实际连接目标。
    """
    route_key      = routing_host or target_host
    action, source = router.match(route_key)

    outbound = outbounds.get(action)
    if outbound is None:
        logger.error("Router returned unknown outbound tag '%s', closing", action)
        await safe_close(local_writer)
        return

    leaf = outbound.resolve_leaf()
    label = f"{routing_host} ({target_host}:{target_port})" if routing_host else f"{target_host}:{target_port}"
    # 组节点把实际用的叶子也打出来，方便排查 urltest 选了谁
    if leaf is not outbound:
        _alog("info", "%s -> %s (via %s)  %s  [%s]",
              outbound.tag, leaf.tag, outbound.type, label, source)
    else:
        _alog("info", "%-8s %s  [%s]", outbound.tag, label, source)

    await outbound.handle(local_reader, local_writer, target_host, target_port)


async def handle_local_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    outbounds: dict[str, Outbound],
    router,
) -> None:
    target = await parse_socks5_request(local_reader, local_writer)
    if target is None:
        await safe_close(local_writer)
        return
    await _dispatch(local_reader, local_writer, target[0], target[1], outbounds, router)


async def handle_tproxy_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    outbounds: dict[str, Outbound],
    router,
) -> None:
    target_host, target_port = local_writer.get_extra_info("sockname")
    domain, buffered = await sniff_domain(local_reader)
    reader = PrefixedReader(local_reader, buffered) if buffered else local_reader
    await _dispatch(reader, local_writer, target_host, target_port, outbounds, router,
                    routing_host=domain)


def _raise_fd_limit() -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            logger.info("File descriptor limit raised: %d -> %d", soft, hard)
    except Exception as e:
        logger.warning("Could not raise fd limit: %s", e)


def _legacy_action_map(outbounds: dict[str, Outbound]) -> dict[str, str]:
    """
    老 CSV rules 的 PROXY/DIRECT/REJECT 关键字 → outbound tag。

    在合成单节点模式下：
      PROXY  → "proxy"  （build_outbounds 合成的 pyrealiy outbound tag）
      DIRECT → "direct" （自动补全的 direct outbound tag）
      REJECT → "block"  （自动补全的 block outbound tag）

    新配置 + 老 CSV rules 的混合：仍提供同名映射，但用户的 outbound tag
    可能不叫 "proxy"——这种情况下需要用结构化 rules 或者显式 tag。
    """
    return {PROXY: "proxy", DIRECT: "direct", REJECT: "block"}


def _check_brutal_kernel(cfg: dict) -> None:
    """
    任意 outbound 设置了 brutal_rate_bps 但内核模块未加载 → 全部置 0 静默回落。
    在 build_outbounds 之前调用（这样合成的 BrutalPool 不会尝试 setsockopt）。
    """
    raw_obs = cfg.get("outbounds") or []
    uses_brutal_top = bool(cfg.get("brutal_rate_bps", 0))
    uses_brutal_any = uses_brutal_top or any(
        isinstance(o, dict) and int(o.get("brutal_rate_bps", 0)) > 0
        for o in raw_obs
    )
    if not uses_brutal_any:
        return
    if brutal.is_available():
        logger.info("TCP Brutal kernel module: available")
        return
    logger.warning("brutal_rate_bps set but kernel module not loaded — falling back to normal TCP")
    cfg["brutal_rate_bps"] = 0
    for o in raw_obs:
        if isinstance(o, dict) and o.get("brutal_rate_bps", 0):
            o["brutal_rate_bps"] = 0


async def main(config_path: str) -> None:
    _raise_fd_limit()
    install_stale_gaierror_handler(asyncio.get_running_loop())

    with open(config_path) as f:
        cfg = json.load(f)

    # 在任何业务日志之前应用 log_levels，否则启动日志仍按旧级别走
    apply_log_levels(cfg)

    global _ACCESS_LOG
    _ACCESS_LOG = bool(cfg.get("access_log", False))
    if _ACCESS_LOG:
        logger.info("access_log enabled (per-connection dispatch logging on)")

    drain_thr = int(cfg.get("drain_threshold", 64 * 1024))
    if drain_thr != 64 * 1024:
        set_drain_threshold(drain_thr)
        logger.info("drain_threshold set to %d bytes", drain_thr)

    # ── 构建 outbound 字典 ────────────────────────────────────────────────
    _check_brutal_kernel(cfg)
    outbounds = build_outbounds(cfg)
    # legacy_action_map 总是提供：即便用户在新 outbounds + CSV rules 混用，
    # 用 PROXY/DIRECT/REJECT 关键字也能继续工作（前提是定义了同名 outbound）。
    # 用户用了自定义 tag（如 "tokyo-1"）的 CSV 行会原样作为 outbound 名查找，
    # valid_actions 校验不通过则规则被警告 + 跳过。
    legacy_map = _legacy_action_map(outbounds)
    logger.info("Outbounds loaded: %s",
                ", ".join(f"{t}({o.type})" for t, o in outbounds.items()))

    # ── 全部 pyrealiy outbound 并行 warmup ────────────────────────────────
    pyrealiy_obs = [o for o in outbounds.values() if isinstance(o, PyrealiyOutbound)]
    if pyrealiy_obs:
        await asyncio.gather(*[o.warmup() for o in pyrealiy_obs], return_exceptions=True)

    # ── geo 数据下载：复用第一个可用 pyrealiy outbound 的 tunnel ─────────
    # 弱网下走自家隧道比直连 GitHub 稳；该 outbound 池空时 geo 模块会自动 fallback 直连
    geo_pool = None
    for o in pyrealiy_obs:
        if o.pool.ready_count > 0:
            geo_pool = o.pool
            logger.info("geo download will tunnel through outbound '%s'", o.tag)
            break
    if geo_pool is None and pyrealiy_obs:
        logger.warning("All pyrealiy outbound pools empty after warmup; geo download will go direct")

    available_site, available_ip = await geo_ensure_all(cfg, pool=geo_pool)

    # ── 构建 router ───────────────────────────────────────────────────────
    router = build_router(
        cfg, available_site, available_ip,
        valid_actions=set(outbounds.keys()),
        legacy_action_map=legacy_map,
    )

    # ── HealthCheck：长闲时主动 probe 兜底 ────────────────────────────────
    health = HealthCheck(outbounds.values())
    health.start()

    socks5_server = await asyncio.start_server(
        lambda r, w: handle_local_connection(r, w, outbounds, router),
        cfg["socks5_host"],
        cfg["socks5_port"],
        limit=262144,
    )
    logger.info("SOCKS5 listening on %s:%d", cfg["socks5_host"], cfg["socks5_port"])

    dns_forwarder = None
    if cfg.get("dns_listen_port", 0):
        try:
            dns_forwarder = DNSForwarder(cfg, router, outbounds)
            await dns_forwarder.start()
        except OSError as e:
            logger.error("DNS forwarder failed to start: %s", e)
            dns_forwarder = None

    tproxy_server = None
    tproxy_port = cfg.get("tproxy_port", 0)
    if tproxy_port:
        try:
            from core import tproxy as _tproxy_mod
            tproxy_server = await _tproxy_mod.start_server(
                "0.0.0.0",
                tproxy_port,
                lambda r, w: handle_tproxy_connection(r, w, outbounds, router),
            )
        except OSError as e:
            logger.error("TProxy failed to start: %s", e)

    try:
        if tproxy_server:
            async with socks5_server, tproxy_server:
                await asyncio.gather(
                    socks5_server.serve_forever(),
                    tproxy_server.serve_forever(),
                )
        else:
            async with socks5_server:
                await socks5_server.serve_forever()
    finally:
        await health.stop()
        if dns_forwarder:
            dns_forwarder.stop()
        for o in outbounds.values():
            await o.stop()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        print("[*] uvloop enabled")
    except ImportError:
        pass

    config = sys.argv[1] if len(sys.argv) > 1 else "config_client.json"
    asyncio.run(main(config))
