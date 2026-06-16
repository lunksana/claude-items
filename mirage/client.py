"""
Mirage 客户端

本地监听 SOCKS5 → 路由判断 → 按命中的 outbound 走（每个 outbound 自管连接池）

多节点 / 自适应选路（sing-box 风格）：
  config_client.json 顶层 "outbounds" 数组定义节点 + 组：
    type=mirage  → 加密隧道叶子节点，独占一个 BrutalPool
    type=direct    → 系统直连
    type=block     → 拒绝
    type=urltest   → 组：选 median latency 最低的 child（带 tolerance 防抖）
    type=fallback  → 组：按顺序选第一个 healthy 的 child

  延迟数据：复用 BrutalPool 每次 build 的握手耗时（无额外探测）；长时间
  无流量时由 HealthCheck 主动 probe 兜底。

可选 TProxy 透明代理（需要 root + iptables TPROXY 规则，参考 README "TProxy 防火墙规则" 一节手配）

老配置（顶层 server_host）：build_outbounds 自动合成单 mirage outbound 'proxy'。
老 rules（CSV 字符串）：PROXY/DIRECT/REJECT 关键字自动映射到 proxy/direct/block 三个 tag。

用法：
  python client.py [config_client.json]
"""


from __future__ import annotations

import asyncio
import json
import os
import resource
import sys

from core.version import __version__
from core.config import load_config, validate_cross_refs, ConfigError
from core.reload import RouterRef, Reloader
from core.time_sync import TimeSync
from core.hello_auth import set_time_provider
from core.socks5 import parse_socks5_request, reply_udp_associate
from core.outbound import build_outbounds, MirageOutbound, DirectOutbound, Outbound
from core.udp_relay import UDPRelay
from core.dns_forwarder import DNSForwarder
from core.sniffer import sniff_domain, PrefixedReader
from core.geosite_cache import ensure_all as geo_ensure_all
from core.router import build_router, PROXY, DIRECT, REJECT
from core.healthcheck import HealthCheck
from core.utils import (get_logger, safe_close, apply_log_levels, apply_log_format,
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
    registry=None,
    inbound_type: str = "Socks5",
    routing_cache=None,
) -> None:
    """
    路由判断 + 委派给命中的 outbound。SOCKS5 / TProxy 两个入口共用。

    routing_host: 用于路由匹配的域名（TProxy sniff 结果）；
                  None 时退化为 target_host。
    target_host:  实际连接目标。
    registry:     ConnectionRegistry，None 时不做统计；非 None 时注册/注销
                  conn_info 并把 byte 回调传给 outbound.handle。
    routing_cache: RoutingCache（None 时不查/不写）。命中即跳过 router.match，
                   显著缩短热门域名的分发开销。
    """
    route_key = routing_host or target_host

    # 决策缓存：命中即跳过规则扫描
    action = None
    source = ""
    if routing_cache is not None:
        cached = routing_cache.get(route_key)
        if cached is not None:
            action = cached
            source = "cached"
    if action is None:
        action, source = router.match(route_key)
        if routing_cache is not None:
            routing_cache.put(route_key, action)

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

    on_up = on_down = None
    conn_id = None
    if registry is not None:
        from core.api.stats import ConnInfo, ConnMetadata, new_conn_id, split_rule
        peer = local_writer.get_extra_info("peername") or ("", 0)
        rule_kind, rule_payload = split_rule(source)
        meta = ConnMetadata(
            network="tcp",
            type=inbound_type,
            source_ip=str(peer[0]) if peer else "",
            source_port=str(peer[1]) if peer else "",
            destination_ip=str(target_host) if not routing_host else "",
            destination_port=str(target_port),
            host=routing_host or str(target_host),
        )
        # chains 顺序：Clash 期望"叶在前，入口在后"（数组首项是实际跑流量的节点）
        chains = [leaf.tag] if leaf is outbound else [leaf.tag, outbound.tag]
        info = ConnInfo(
            id=new_conn_id(), metadata=meta, chains=chains,
            rule=rule_kind, rule_payload=rule_payload,
        )
        registry.register(info)
        on_up, on_down = registry.make_callbacks(info)
        conn_id = info.id

    try:
        await outbound.handle(local_reader, local_writer, target_host, target_port,
                              on_up=on_up, on_down=on_down)
    finally:
        if registry is not None and conn_id is not None:
            registry.unregister(conn_id)


async def handle_local_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    outbounds: dict[str, Outbound],
    router,
    udp_cfg: dict,
    registry=None,
    routing_cache=None,
) -> None:
    parsed = await parse_socks5_request(local_reader, local_writer)
    if parsed is None:
        await safe_close(local_writer)
        return
    cmd, host, port = parsed
    if cmd == "tcp":
        await _dispatch(local_reader, local_writer, host, port, outbounds, router,
                        registry=registry, routing_cache=routing_cache)
    elif cmd == "udp":
        await _dispatch_udp(local_reader, local_writer, outbounds, router, udp_cfg,
                            registry=registry)
    else:
        await safe_close(local_writer)


async def _dispatch_udp(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    outbounds: dict[str, Outbound],
    router,
    udp_cfg: dict,
    registry=None,
) -> None:
    """
    SOCKS5 UDP ASSOCIATE 分派。

    路由：UDP 走 router 的 final 动作（拿到的不是具体目标域名，所以 router.match
    没法按目标分流；目标在每个 UDP 包的 frame 里）。final 一般是 'auto' / 'proxy'
    / 'direct'，解析到的 leaf 类型决定 UDP 走加密隧道还是本地直发：
      mirage leaf → acquire tunnel，发 UDP 模式哨兵 b"\\x00"，启动 UDPRelay 带 tunnel
      direct leaf   → 启动 UDPRelay 不带 tunnel，本地 socket 直发
      其他          → 回 SOCKS5 错误并关
    """
    # 解析 FINAL action 拿 leaf
    default_action = router.default
    outbound = outbounds.get(default_action)
    leaf = outbound.resolve_leaf() if outbound is not None else None

    bind_host = udp_cfg.get("udp_relay_host", "127.0.0.1")
    idle_timeout = float(udp_cfg.get("udp_idle_timeout", 60))

    server_writer = None
    tunnel = None

    if isinstance(leaf, MirageOutbound):
        ready = await leaf.acquire_tunnel()
        if ready is None:
            logger.error("UDP relay: no tunnel from outbound '%s'", leaf.tag)
            await safe_close(local_writer)
            return
        tunnel = ready.tunnel
        server_writer = ready.writer
        # UDP-mode 哨兵：单字节 0x00（server.py 看到首字节 == 0 进 UDP 路径）
        try:
            await tunnel.send(b"\x00")
        except Exception as e:
            logger.error("UDP relay: bootstrap failed: %s", e)
            await safe_close(server_writer)
            await safe_close(local_writer)
            return
    elif isinstance(leaf, DirectOutbound):
        pass   # tunnel 留 None；UDPRelay 走 direct 路径
    else:
        # block / 其他类型：拒绝
        logger.info("UDP ASSOCIATE rejected: outbound '%s' leaf=%s not supported",
                    default_action, type(leaf).__name__ if leaf else None)
        await safe_close(local_writer)
        return

    on_up = on_down = None
    conn_id = None
    if registry is not None and leaf is not None:
        from core.api.stats import ConnInfo, ConnMetadata, new_conn_id
        peer = local_writer.get_extra_info("peername") or ("", 0)
        meta = ConnMetadata(
            network="udp",
            type="Socks5",
            source_ip=str(peer[0]) if peer else "",
            source_port=str(peer[1]) if peer else "",
            destination_ip="0.0.0.0",   # UDP-ASSOCIATE 目标在每包 frame 里
            destination_port="0",
            host="udp-associate",
        )
        chains = [leaf.tag] if leaf is outbound else [leaf.tag, outbound.tag]
        info = ConnInfo(
            id=new_conn_id(), metadata=meta, chains=chains,
            rule="Match", rule_payload=default_action,
        )
        registry.register(info)
        on_up, on_down = registry.make_callbacks(info)
        conn_id = info.id

    relay = UDPRelay(local_reader, local_writer, tunnel, server_writer,
                     bind_host=bind_host, idle_timeout=idle_timeout,
                     on_up=on_up, on_down=on_down)
    try:
        bnd_host, bnd_port = await relay.start()
        await reply_udp_associate(local_writer, bnd_host, bnd_port)
        _alog("info", "UDP-ASSOC bnd=%s:%d leaf=%s", bnd_host, bnd_port,
              leaf.tag if leaf else "?")
        await relay.run()
    except Exception as e:
        logger.debug("UDP relay error: %s", e)
    finally:
        if registry is not None and conn_id is not None:
            registry.unregister(conn_id)
        if server_writer is not None:
            await safe_close(server_writer)
        await safe_close(local_writer)


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
      PROXY  → "proxy"  （build_outbounds 合成的 mirage outbound tag）
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

    cfg = load_config(config_path)

    # 在任何业务日志之前应用日志格式 + 级别，否则启动日志仍按旧设置走
    apply_log_format(cfg)
    apply_log_levels(cfg)

    logger.info("Mirage client v%s", __version__)

    # 时钟同步：阻塞 ≤5s 拉一次 NTP/HTTPS 时间，避免 VPS 时钟漂移 > 60s
    # 让所有 token 被服务端误判超时。失败不挂掉业务、后台周期重试。
    # set_time_provider 永远注入：TimeSync 未启用时 offset=0、等价于系统时钟。
    set_time_provider(TimeSync.corrected_time)
    time_sync = TimeSync(cfg)
    if time_sync.enabled:
        await time_sync.initial_sync()
        time_sync.start()

    global _ACCESS_LOG
    _tuning = cfg.get("tuning") or {}
    _ACCESS_LOG = bool(_tuning.get("access_log", cfg.get("access_log", False)))
    if _ACCESS_LOG:
        logger.info("access_log enabled (per-connection dispatch logging on)")

    drain_thr = int(cfg.get("drain_threshold", 64 * 1024))
    if drain_thr != 64 * 1024:
        set_drain_threshold(drain_thr)
        logger.info("drain_threshold set to %d bytes", drain_thr)

    # ── 构建 outbound 字典 ────────────────────────────────────────────────
    _check_brutal_kernel(cfg)
    outbounds = build_outbounds(cfg)
    validate_cross_refs(cfg, set(outbounds.keys()))
    # legacy_action_map 总是提供：即便用户在新 outbounds + CSV rules 混用，
    # 用 PROXY/DIRECT/REJECT 关键字也能继续工作（前提是定义了同名 outbound）。
    # 用户用了自定义 tag（如 "tokyo-1"）的 CSV 行会原样作为 outbound 名查找，
    # valid_actions 校验不通过则规则被警告 + 跳过。
    legacy_map = _legacy_action_map(outbounds)
    logger.info("Outbounds loaded: %s",
                ", ".join(f"{t}({o.type})" for t, o in outbounds.items()))

    # ── 全部 mirage outbound 并行 warmup ────────────────────────────────
    mirage_obs = [o for o in outbounds.values() if isinstance(o, MirageOutbound)]
    if mirage_obs:
        await asyncio.gather(*[o.warmup() for o in mirage_obs], return_exceptions=True)

    # ── 构建 router：先用空 geo 立即建好，让入站监听不必等 geo 下载 ───────
    # geo 数据（数 MB 的 .dat）下载可能很慢；放后台异步拉，完成后原子换 router。
    # 这样首次运行 SOCKS5/mixed 端口立刻可用，domain/ip_cidr 规则即时生效，
    # rule_set/geoip 规则在 geo 到位后自动补上。
    available_site: dict = {}
    available_ip: dict = {}
    router_inner = build_router(
        cfg, available_site, available_ip,
        valid_actions=set(outbounds.keys()),
        legacy_action_map=legacy_map,
    )
    # 包一层 RouterRef 让热加载 / geo 后台任务可以原子替换 inner
    router = RouterRef(router_inner)

    # ── HealthCheck：长闲时主动 probe 兜底 ────────────────────────────────
    health = HealthCheck(outbounds.values())
    health.start()

    # UDP relay 配置：默认走 SOCKS5 控制连接绑的地址，端口由 OS 分配
    udp_cfg = {
        "udp_relay_host":   cfg.get("udp_relay_host", cfg.get("socks5_host", "127.0.0.1")),
        "udp_idle_timeout": cfg.get("udp_idle_timeout", 60),
    }

    # ── Clash API 连接统计：仅在 api.listen 配置时启用 ─────────────────────
    api_registry = None
    if (cfg.get("api") or {}).get("listen"):
        from core.api.stats import ConnectionRegistry
        api_registry = ConnectionRegistry()

    # ── 路由决策缓存：默认开（命中跳过 router.match）─────────────────────
    routing_cache = None
    _tuning_cache = (cfg.get("tuning") or {}).get("routing_cache") or {}
    if _tuning_cache.get("enabled", True):
        from core.decision_cache import RoutingCache
        routing_cache = RoutingCache(
            max_entries=int(_tuning_cache.get("max_entries", 10000)),
            ttl_sec=float(_tuning_cache.get("ttl_sec", 3600)),
        )

    # ── 入站监听：socks5 / http / mixed，按 cfg.inbounds 起多个 listener ──
    # 没显式 inbounds 时合成一个 socks5（用 legacy 顶层 socks5_host/port）
    inbounds_cfg = cfg.get("inbounds")
    if not inbounds_cfg:
        inbounds_cfg = [{
            "type":   "socks5",
            "listen": f"{cfg.get('socks5_host', '127.0.0.1')}:{cfg.get('socks5_port', 1080)}",
        }]

    from core.http_inbound import handle_http_connection, handle_mixed_connection
    _INBOUND_HANDLERS = {
        "socks5": handle_local_connection,
        "http":   handle_http_connection,
        "mixed":  handle_mixed_connection,
    }

    inbound_servers = []
    for ib in inbounds_cfg:
        ib_type = ib["type"]
        listen = ib["listen"]
        host, port_s = listen.rsplit(":", 1)
        port = int(port_s)
        fn = _INBOUND_HANDLERS.get(ib_type)
        if fn is None:
            logger.warning("Unsupported inbound type '%s', skipping", ib_type)
            continue
        # 闭包：fn 用默认参数防 late-binding；其他变量在 main() 里全程不变
        async def _make_handler(reader, writer, _fn=fn):
            await _fn(reader, writer, outbounds, router, udp_cfg,
                      registry=api_registry, routing_cache=routing_cache)
        server = await asyncio.start_server(_make_handler, host, port, limit=262144)
        inbound_servers.append(server)
        logger.info("%s listening on %s:%d", ib_type.upper(), host, port)

    # DNS 响应缓存：仅在 DNS forwarder 启用时建
    dns_cache = None
    if cfg.get("dns_listen_port", 0):
        _tuning_dns = (cfg.get("tuning") or {}).get("dns_cache") or {}
        if _tuning_dns.get("enabled", True):
            from core.decision_cache import DnsCache
            dns_cache = DnsCache(max_entries=int(_tuning_dns.get("max_entries", 10000)))

    dns_forwarder = None
    if cfg.get("dns_listen_port", 0):
        try:
            dns_forwarder = DNSForwarder(cfg, router, outbounds,
                                         routing_cache=routing_cache,
                                         dns_cache=dns_cache)
            await dns_forwarder.start()
        except OSError as e:
            logger.error("DNS forwarder failed to start: %s", e)
            dns_forwarder = None

    api_server = None
    log_bc = None
    api_ctx = None
    if (cfg.get("api") or {}).get("listen"):
        from core.api import APIServer
        from core.api.server import APIContext
        from core.api.ws_endpoints import LogBroadcaster
        log_bc = LogBroadcaster()
        log_bc.attach()
        api_ctx = APIContext(
            version=__version__,
            cfg=cfg,
            outbounds=outbounds,
            router_engine=router,
            registry=api_registry,
            log_broadcaster=log_bc,
            routing_cache=routing_cache,
            dns_cache=dns_cache,
        )
        api_server = APIServer(cfg, api_ctx)
        try:
            await api_server.start()
        except OSError as e:
            logger.error("API failed to start: %s", e)
            api_server = None
            log_bc.detach()
            log_bc = None

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

    # ── Reloader：SIGHUP + POST /configs 都用这个 ─────────────────────────
    reloader = Reloader(config_path, cfg)
    reloader.router_ref = router
    reloader.outbounds = outbounds
    reloader.routing_cache = routing_cache
    reloader.dns_cache = dns_cache
    reloader.dns_forwarder = dns_forwarder
    reloader.available_site = available_site
    reloader.available_ip = available_ip
    reloader.legacy_action_map = legacy_map

    def _refresh_access_log(new_cfg: dict) -> None:
        global _ACCESS_LOG
        t = new_cfg.get("tuning") or {}
        _ACCESS_LOG = bool(t.get("access_log", new_cfg.get("access_log", False)))
    reloader.add_tuning_handler(_refresh_access_log)

    if api_ctx is not None:
        api_ctx.reloader = reloader
        reloader.api_ctx = api_ctx   # B1：reload 后同步 api_ctx.cfg

    # ── geo 后台任务：首次下载 + 周期刷新，完成后原子换 router ──────────────
    # 解决两件事：
    #   1) 入站监听不再被首次 geo 下载阻塞（见上方 router 用空 geo 立即构建）
    #   2) 长跑进程也会按 geosite_update_days 周期复查刷新（旧设计只在启动下一次，
    #      reload 也不重下，导致 geo 数据永远停在首次快照）
    def _pick_geo_pool():
        # 每轮重新挑：池可能在运行中重连/恢复
        for o in mirage_obs:
            if o.pool.ready_count > 0:
                return o.pool, o.tag
        return None, None

    async def _geo_background() -> None:
        # 默认 48h 复查一次；ensure_all 内部按 update_days 决定是否真的重下，
        # 缓存新鲜时只是 stat + 返回旧路径，开销极小
        refresh_sec = float(cfg.get("geo_refresh_check_sec", 48 * 3600))
        first = True
        while True:
            try:
                pool, tag = _pick_geo_pool()
                if pool is not None:
                    logger.info("geo download will tunnel through outbound '%s'", tag)
                elif mirage_obs:
                    logger.warning("All mirage pools empty; geo download will go direct")
                site, ip = await geo_ensure_all(cfg, pool=pool)
                if site or ip:
                    new_inner = build_router(
                        cfg, site, ip,
                        valid_actions=set(outbounds.keys()),
                        legacy_action_map=legacy_map,
                    )
                    router.replace(new_inner)
                    reloader.available_site = site
                    reloader.available_ip = ip
                    # 路由规则变了：清决策缓存，避免旧决策粘住
                    rc_n = routing_cache.invalidate() if routing_cache is not None else 0
                    dc_n = dns_cache.invalidate() if dns_cache is not None else 0
                    logger.info("geo router rebuilt: %d site src, %d ip src "
                                "(routing_cache cleared %d, dns_cache cleared %d)",
                                len(site), len(ip), rc_n, dc_n)
                elif first:
                    logger.warning("geo download produced no data; geo rules inactive "
                                   "until next refresh (in %.0fh)", refresh_sec / 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                # geo_ensure_all 内部已吞掉网络/下载失败（返回空 dict），所以走到
                # 这里多半是真 bug（如 build_router 内部异常）——保留 traceback，
                # 否则 48h 间隔的后台任务里一行 warning 几乎不可能被发现
                logger.exception("geo background task unexpected error")
            first = False
            try:
                await asyncio.sleep(refresh_sec)
            except asyncio.CancelledError:
                raise

    geo_task = asyncio.create_task(_geo_background())

    # SIGHUP → schedule reload as task
    import signal
    def _sighup_handler():
        logger.info("SIGHUP received, scheduling config reload")
        asyncio.create_task(reloader.reload())
    try:
        asyncio.get_event_loop().add_signal_handler(signal.SIGHUP, _sighup_handler)
        logger.info("SIGHUP handler installed (kill -HUP %d to reload)", os.getpid())
    except (NotImplementedError, AttributeError):
        # Windows 不支持 SIGHUP；忽略
        pass

    try:
        import contextlib
        all_servers = list(inbound_servers)
        if tproxy_server:
            all_servers.append(tproxy_server)
        async with contextlib.AsyncExitStack() as stack:
            for s in all_servers:
                await stack.enter_async_context(s)
            await asyncio.gather(*(s.serve_forever() for s in all_servers))
    finally:
        geo_task.cancel()
        try:
            await geo_task
        except (asyncio.CancelledError, Exception):
            pass
        await time_sync.stop()
        await health.stop()
        if api_server:
            await api_server.stop()
        if log_bc:
            log_bc.detach()
        if dns_forwarder:
            dns_forwarder.stop()
        for o in outbounds.values():
            await o.stop()


if __name__ == "__main__":
    # 接受 PYREALIY_NO_UVLOOP 作为 legacy 别名（0.4.35 前的环境变量名）
    if os.environ.get("MIRAGE_NO_UVLOOP") == "1" or os.environ.get("PYREALIY_NO_UVLOOP") == "1":
        print("[*] uvloop disabled (asyncio default)")
    else:
        try:
            import uvloop
            uvloop.install()
            print("[*] uvloop enabled")
        except ImportError:
            pass

    config = sys.argv[1] if len(sys.argv) > 1 else "config_client.json"
    asyncio.run(main(config))
