"""
PyReality 客户端

本地监听 SOCKS5 → 路由判断 → PROXY 走预建隧道池 / DIRECT 直连目标

可选 TProxy 透明代理（需要 root + iptables TPROXY 规则，由 setup.py 生成）：
  配置 tproxy_port 后同时启动 TProxy 监听器，无需应用层配合即可透明代理所有 TCP 流量

Brutal 多连接策略：
  brutal_rate_bps  = 每条连接的速率（建议 5~10 Mbps）
  brutal_pool_size = 预建连接数（建议 10~20）
  总吞吐上限       ≈ brutal_pool_size × brutal_rate_bps

用法：
  python client.py [config_client.json]
"""


from __future__ import annotations

import asyncio
import ipaddress
import json
import resource
import socket
import sys

from core.socks5 import parse_socks5_request
from core.conn_pool import BrutalPool
from core.dns_forwarder import DNSForwarder
from core.sniffer import sniff_domain, PrefixedReader
from core.geosite_cache import ensure_all as geo_ensure_all
from core.router import build_router, DIRECT, REJECT
from core.utils import (get_logger, pack_address, relay, safe_close,
                        install_stale_gaierror_handler, set_drain_threshold,
                        get_drain_threshold)
from core import brutal

logger = get_logger("client")


# ── 启动期辅助 ────────────────────────────────────────────────────────────────

def _resolve_server_host(cfg: dict) -> None:
    """
    把 cfg['server_host'] 中的域名一次性解析为 IP 并替换。

    每条 pool 隧道都对 server_host 做一次 open_connection；如果它是域名，
    asyncio 在线程池执行器里发 getaddrinfo —— 这个 future 不可取消，
    pool 的 wait_for 超时后没人接住它的结果，gaierror 就成了 stale future
    的未消费异常，asyncio 抱怨"Future exception was never retrieved"。

    启动一次性解析既消除噪音根源，又能在启动期快速暴露"客户端连不上服务端"。
    """
    # strip 防止手编 JSON 时混入空格 / BOM：否则 ipaddress 解析失败 → 当域名 →
    # getaddrinfo 也失败 → 错误信息会指向 "DNS 不通"，把用户引到错的方向
    raw  = cfg["server_host"]
    host = raw.strip()
    if host != raw:
        logger.warning("server_host %r had surrounding whitespace, stripped to %r", raw, host)
    cfg["server_host"] = host

    try:
        ipaddress.ip_address(host)
        return                                  # 已经是 IP
    except ValueError:
        pass

    # AF_UNSPEC 同时拿 A 和 AAAA，IPv6-only 服务也能用；
    # IPv4 优先（兼容性好），实在没 v4 再用 v6
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        v4 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET})
        v6 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET6})
    except socket.gaierror as e:
        logger.error("Cannot resolve server_host %s: %s — 请检查 DNS / hosts / VPN", host, e)
        raise SystemExit(1)

    ips = v4 + v6                               # v4 优先
    if not ips:
        logger.error("No A/AAAA record for server_host %s", host)
        raise SystemExit(1)

    cfg["_server_host_original"] = host         # 保留原始域名留作日志
    cfg["server_host"] = ips[0]
    if len(ips) > 1:
        logger.info("server_host %s -> %d IPs, using %s (others: %s)",
                    host, len(ips), ips[0], ", ".join(ips[1:]))
    else:
        logger.info("server_host %s -> %s", host, ips[0])


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
    pool: BrutalPool,
    router,
    routing_host: str | None = None,
) -> None:
    """
    路由判断 + 转发，由 SOCKS5 和 TProxy 两个入口共用。

    routing_host: 用于路由匹配的域名（TProxy sniff 结果）；
                  None 时退化为 target_host（IP 或 SOCKS5 提供的域名）。
    target_host:  实际连接目标（TProxy 模式下始终是原始 IP）。
    """
    route_key      = routing_host or target_host
    action, source = router.match(route_key)

    label = f"{routing_host} ({target_host}:{target_port})" if routing_host else f"{target_host}:{target_port}"

    if action == REJECT:
        _alog("info", "REJECT  %s  [%s]", label, source)
        await safe_close(local_writer)
        return

    if action == DIRECT:
        _alog("info", "DIRECT  %s  [%s]", label, source)
        try:
            target_reader, target_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except Exception as e:
            logger.error("Direct connect %s:%d failed: %s", target_host, target_port, e)
            await safe_close(local_writer)
            return
        await relay(local_reader, target_writer, target_reader, local_writer)
        return

    # ── 代理分支 ─────────────────────────────────────────────────────────────
    _alog("info", "PROXY   %s  [%s]", label, source)

    ready = await pool.acquire()
    if ready is None:
        logger.error("No available tunnel for %s:%d", target_host, target_port)
        await safe_close(local_writer)
        return

    tunnel = ready.tunnel
    server_writer = ready.writer

    try:
        await tunnel.send(pack_address(target_host, target_port))
    except Exception as e:
        logger.error("Failed to send target address: %s", e)
        await safe_close(server_writer)
        await safe_close(local_writer)
        return

    async def local_to_tunnel():
        try:
            while True:
                data = await local_reader.read(65536)
                if not data:
                    break
                await tunnel.send(data)
        except Exception:
            pass
        finally:
            # 主动关方：先发 TLS close_notify alert 再 FIN，外观更像真实 HTTPS 关闭
            await tunnel.send_close_notify()
            await safe_close(server_writer)

    async def tunnel_to_local():
        try:
            while True:
                data = await tunnel.recv()
                local_writer.write(data)
                if local_writer.transport.get_write_buffer_size() > get_drain_threshold():
                    await local_writer.drain()
        except Exception:
            pass
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


async def handle_local_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    pool: BrutalPool,
    router,
) -> None:
    target = await parse_socks5_request(local_reader, local_writer)
    if target is None:
        await safe_close(local_writer)
        return
    await _dispatch(local_reader, local_writer, target[0], target[1], pool, router)


async def handle_tproxy_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    pool: BrutalPool,
    router,
) -> None:
    # TProxy 模式：内核将原始目标地址写入 sockname，无需协议握手
    target_host, target_port = local_writer.get_extra_info("sockname")

    # Sniff 初始字节，尝试提取域名以启用 GEOSITE/DOMAIN 规则；
    # buffered 必须放回 reader 前缀，确保数据完整到达目标
    domain, buffered = await sniff_domain(local_reader)
    reader = PrefixedReader(local_reader, buffered) if buffered else local_reader

    await _dispatch(reader, local_writer, target_host, target_port, pool, router,
                    routing_host=domain)


def _raise_fd_limit() -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            logger.info("File descriptor limit raised: %d -> %d", soft, hard)
    except Exception as e:
        logger.warning("Could not raise fd limit: %s", e)


async def main(config_path: str) -> None:
    _raise_fd_limit()

    # 把可能漏接的 gaierror 降到 DEBUG（详见 install_stale_gaierror_handler 注释）
    install_stale_gaierror_handler(asyncio.get_running_loop())

    with open(config_path) as f:
        cfg = json.load(f)

    # access_log 开关：默认关，开后才打 dispatch INFO 日志
    global _ACCESS_LOG
    _ACCESS_LOG = bool(cfg.get("access_log", False))
    if _ACCESS_LOG:
        logger.info("access_log enabled (per-connection dispatch logging on)")

    # drain 阈值：默认 64KB，跨境高 BDP 可调到 256KB~1MB
    drain_thr = int(cfg.get("drain_threshold", 64 * 1024))
    if drain_thr != 64 * 1024:
        set_drain_threshold(drain_thr)
        logger.info("drain_threshold set to %d bytes", drain_thr)

    # 启动期把 server_host 域名解析为 IP，省下每次 build 的 getaddrinfo + 噪音
    _resolve_server_host(cfg)

    rate_bps  = cfg.get("brutal_rate_bps", 0)
    pool_size = cfg.get("brutal_pool_size", 10)

    if rate_bps:
        if brutal.is_available():
            logger.info(
                "TCP Brutal: ON | per-conn %.0f Mbps × %d conns = %.0f Mbps max",
                rate_bps / 1e6, pool_size, rate_bps * pool_size / 1e6,
            )
        else:
            logger.warning(
                "brutal_rate_bps is set but kernel module not found — "
                "falling back to normal TCP. Run setup.py to install."
            )
            cfg["brutal_rate_bps"] = 0

    # 先建池：geo 数据下载也要复用同一份隧道（弱网下避免直连 GitHub 慢/失败）
    pool = BrutalPool(cfg)
    ready_count = await pool.warmup()

    # warmup 0 说明服务端不通；继续把 pool 传给 geo 只会反复尝试 + acquire 超时，
    # 单文件最多浪费 ~75s。直接禁用 tunnel 路径走 direct，省启动时间。
    pool_for_geo = pool if ready_count > 0 else None
    if pool_for_geo is None:
        logger.warning("Pool empty after warmup; geo download will go direct only")

    available_site, available_ip = await geo_ensure_all(cfg, pool=pool_for_geo)
    router = build_router(cfg, available_site, available_ip)

    socks5_server = await asyncio.start_server(
        lambda r, w: handle_local_connection(r, w, pool, router),
        cfg["socks5_host"],
        cfg["socks5_port"],
        limit=262144,
    )
    logger.info(
        "SOCKS5 listening on %s:%d  (pool: %d conns ready)",
        cfg["socks5_host"],
        cfg["socks5_port"],
        pool_size,
    )

    dns_forwarder = None
    if cfg.get("dns_listen_port", 0):
        try:
            dns_forwarder = DNSForwarder(cfg, router, pool)
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
                lambda r, w: handle_tproxy_connection(r, w, pool, router),
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
        if dns_forwarder:
            dns_forwarder.stop()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        print("[*] uvloop enabled")
    except ImportError:
        pass

    config = sys.argv[1] if len(sys.argv) > 1 else "config_client.json"
    asyncio.run(main(config))
