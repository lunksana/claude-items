"""
Mirage 服务端

监听原始 TCP（无自有证书）。
启动时预热握手缓存，之后对所有连接在 ClientHello 阶段即完成判断：
  合法客户端 → 直接建代理信道（零额外延迟）
  GFW 探测   → 本地回放缓存的真实握手记录（零额外延迟）

用法：
  python server.py [config_server.json]
"""


from __future__ import annotations

import asyncio

import json

import os

import socket

import sys


from core.camouflage import server_read_hello_and_decide

from core.config import load_config

from core.handshake_cache import HandshakeCache

from core.hello_auth import TokenReplayCache

from core.tunnel import EncryptedTunnel

from core.egress import DefaultEgress, MarkedEgress, Egress

from core.router import build_router

from core.geosite_cache import ensure_all as geo_ensure_all

from core.utils import (get_logger, unpack_address, safe_close, apply_log_levels, apply_log_format,
                        install_stale_gaierror_handler, set_drain_threshold,
                        get_drain_threshold, wait_both_with_grace, set_keepalive, raise_fd_limit,
                        install_uvloop_if_available)
from core.udp_relay import handle_udp_tunnel
from core.version import __version__
from core.time_sync import TimeSync
from core.hello_auth import set_time_provider


_ACCESS_LOG = False                       # 由 main() 从 cfg 读出后设置
_IDLE_TIMEOUT_SEC: float = 1800.0         # tunnel.recv 等 pack_address 的硬上限（DoS 兜底）
_MAX_CONNS_PER_IP: int   = 100            # 单 IP 同时持有的认证连接上限
_TCP_KEEPALIVE: bool     = True           # SO_KEEPALIVE + TCP_KEEPIDLE/INTVL/CNT

_CONN_COUNT_BY_IP: dict[str, int] = {}    # ip → 当前 in-flight 计数


def _alog(level: str, fmt: str, *args) -> None:
    """每连接的 dispatch 日志：默认关，cfg['access_log']=true 时开"""
    if not _ACCESS_LOG:
        return
    getattr(logger, level)(fmt, *args)


def _acquire_ip_slot(client_ip: str) -> bool:
    """单 IP 并发上限。返回 False 时直接拒绝连接。asyncio 单线程，原子。"""
    count = _CONN_COUNT_BY_IP.get(client_ip, 0)
    if count >= _MAX_CONNS_PER_IP:
        return False
    _CONN_COUNT_BY_IP[client_ip] = count + 1
    return True


def _release_ip_slot(client_ip: str) -> None:
    c = _CONN_COUNT_BY_IP.get(client_ip, 1) - 1
    if c <= 0:
        _CONN_COUNT_BY_IP.pop(client_ip, None)
    else:
        _CONN_COUNT_BY_IP[client_ip] = c


from core.stats import StatsStore

from core.admin import start_admin

from core import brutal

logger = get_logger("server")


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    cfg: dict,
    cache: HandshakeCache,
    replay_cache: TokenReplayCache,
    store: StatsStore,
    egresses: dict,                         # {name: Egress}
    egress_router=None,                     # None = 全部走 default
) -> None:
    peer      = client_writer.get_extra_info("peername")
    client_ip = peer[0] if peer else "unknown"

    # 封锁 IP 直接断开（在握手前，避免浪费资源）
    if store.is_blocked(client_ip):
        logger.info("Blocked %s", client_ip)
        await safe_close(client_writer)
        return

    # 单 IP 并发上限：在 camouflage 之前就拒绝，避免攻击者用慢速 ClientHello 撑爆 fd
    if not _acquire_ip_slot(client_ip):
        logger.warning("Per-IP cap %d reached for %s, rejecting", _MAX_CONNS_PER_IP, client_ip)
        await safe_close(client_writer)
        return

    try:
        _alog("info", "New connection from %s", peer)

        # 阶段1：读 ClientHello，认证决策 + 握手模拟（含重放检测）
        ok, client_random = await server_read_hello_and_decide(
            client_reader, client_writer, cfg["password"], cache, replay_cache
        )
        if not ok:
            return  # 探测连接已由 camouflage 模块处理完毕

        # 认证通过的合法客户端：开 TCP keepalive 检测死连接（NAT/路由器断了但没发 FIN）
        sock = client_writer.get_extra_info("socket")
        if _TCP_KEEPALIVE and sock:
            set_keepalive(sock)

        # 合法客户端：对该 TCP 连接设置 Brutal 速率
        rate_bps = cfg.get("brutal_rate_bps", 0)
        if rate_bps and sock:
            brutal.set_rate(sock, rate_bps)

        # 阶段2：派生会话密钥（client_random 已从 ClientHello 提取，无需额外握手）
        tunnel = EncryptedTunnel(client_reader, client_writer, cfg["password"])
        await tunnel.do_handshake_as_responder(client_random)

        # 阶段3：读取目标地址 —— 带 idle timeout 防"已认证但永不发数据"的 DoS
        try:
            addr_packet = await asyncio.wait_for(
                tunnel.recv(),
                timeout=_IDLE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            _alog("info", "Idle %ds without target address from %s, closing",
                  int(_IDLE_TIMEOUT_SEC), peer)
            await safe_close(client_writer)
            return
        except Exception:
            logger.debug("Connection from %s closed before sending target address", peer)
            await safe_close(client_writer)
            return

        # ── UDP 模式检测 ──
        # 0.4.10 起：客户端发首包 b"\x00"（host_len=0）表示"这条 tunnel 走 UDP"。
        # 老客户端发的 pack_address(host, port) 首字节是 host_len，最小 IP 字符串
        # "0.0.0.0" host_len=7，永不为 0 → 完全向后兼容。
        if addr_packet and addr_packet[0] == 0:
            conn = store.register(client_ip, "udp", 0, client_writer)
            _alog("info", "Proxy %s -> UDP mode [id=%d]", peer, conn.id)
            # 闭包计数器：比 setattr lambda 干净，少一次属性 setattr 调用
            def _add_up(n):   conn.bytes_up   += n
            def _add_down(n): conn.bytes_down += n
            try:
                await handle_udp_tunnel(tunnel,
                                        on_byte_in=_add_up,
                                        on_byte_out=_add_down)
            finally:
                store.unregister(conn)
                await safe_close(client_writer)
            return

        target_host, target_port, _conn_unused = unpack_address(addr_packet)
        conn = store.register(client_ip, target_host, target_port, client_writer)

        # ── egress 选择 ──
        egress: Egress = egresses["DIRECT"]
        egress_src = "default"
        if egress_router is not None:
            egress_name, egress_src = egress_router.match(target_host)
            egress = egresses.get(egress_name) or egresses["DIRECT"]

        _alog("info", "Proxy %s -> %s:%d [id=%d, egress=%s, %s]",
              peer, target_host, target_port, conn.id, egress.name, egress_src)

        # 阶段4：连接目标，双向中继
        try:
            target_reader, target_writer = await egress.open_connection(target_host, target_port)
        except Exception as e:
            logger.error("Cannot connect to %s:%d via egress %s: %s",
                         target_host, target_port, egress.name, e)
            await safe_close(client_writer)
            store.unregister(conn)
            return

        # 中继 leg 终止时的异常默认静默（对端 RST / FIN / Timeout 是常规事件），
        # 但开启 logger("server") 的 DEBUG 级别就能看到类型 + 消息，便于排查
        # "连接莫名其妙就断了" 这类问题。conn.id + target 写在日志里做对账。
        #
        # 关闭顺序：内层只发 FIN/close_notify，由外层 wait_both_with_grace +
        # safe_close 兜底，避免 FIN→RST 指纹（详见 utils.safe_close）。
        async def tunnel_to_target():
            try:
                while True:
                    data = await tunnel.recv()
                    conn.bytes_up += len(data)
                    target_writer.write(data)
                    if target_writer.transport.get_write_buffer_size() > get_drain_threshold():
                        await target_writer.drain()
            except Exception as e:
                logger.debug("relay id=%d %s:%d tunnel→target ended: %s: %s",
                             conn.id, target_host, target_port, type(e).__name__, e)
            # 对 target 发 FIN，让目标尽快也 EOF；不 close（外层统一）
            try:
                if target_writer.can_write_eof():
                    target_writer.write_eof()
            except Exception:
                pass

        async def target_to_tunnel():
            try:
                while True:
                    data = await target_reader.read(65536)
                    if not data:
                        break
                    conn.bytes_down += len(data)
                    await tunnel.send(data)
            except Exception as e:
                logger.debug("relay id=%d %s:%d target→tunnel ended: %s: %s",
                             conn.id, target_host, target_port, type(e).__name__, e)
            # TLS 层的"FIN"：发 close_notify 让客户端的 tunnel.recv 自然 EOF
            await tunnel.send_close_notify()

        task_a = asyncio.create_task(tunnel_to_target())
        task_b = asyncio.create_task(target_to_tunnel())
        try:
            # 优雅关：等一方向结束后给另一方向最多 2s 自然退出
            await wait_both_with_grace(task_a, task_b)
        finally:
            # close 前 drain 各接收缓冲（防 close → RST，详见 utils.safe_close）：
            #   tunnel 底层 = 客户端→服务端的加密 record（含 close_notify 后可能跟着的字节）
            #   target_reader = target 在 grace 期间继续推过来的下行字节
            await tunnel.drain_recv(0.5)
            await safe_close(target_writer, target_reader)
            await safe_close(client_writer)
            store.unregister(conn)

        _alog("info", "Connection from %s closed [id=%d]", peer, conn.id)

    finally:
        # 无论怎么走（正常返回 / 异常 / DoS 兜底 timeout），都释放 IP 槽
        _release_ip_slot(client_ip)


async def main(config_path: str) -> None:
    raise_fd_limit()

    # 静音 stale getaddrinfo future 的噪音（详见 utils.install_stale_gaierror_handler）
    install_stale_gaierror_handler(asyncio.get_running_loop())

    cfg = load_config(config_path)

    # 在任何业务日志之前应用日志格式 + 级别，否则启动日志仍按旧设置走
    apply_log_format(cfg)
    apply_log_levels(cfg)

    logger.info("Mirage server v%s", __version__)

    # 时钟同步：阻塞 ≤5s 拉一次 NTP/HTTPS 时间，避免 VPS 时钟漂移 > 60s
    # 让客户端的合法 token 被误判为超时。失败不挂掉业务、后台周期重试。
    # 服务端多数有 chrony，但我们自己同步能避免对外部依赖的硬要求。
    set_time_provider(TimeSync.corrected_time)
    time_sync = TimeSync(cfg)
    if time_sync.enabled:
        await time_sync.initial_sync()
        time_sync.start()

    # access_log 开关：默认关，开后才打 dispatch INFO 日志
    global _ACCESS_LOG, _IDLE_TIMEOUT_SEC, _MAX_CONNS_PER_IP, _TCP_KEEPALIVE
    _ACCESS_LOG = bool(cfg.get("access_log", False))
    if _ACCESS_LOG:
        logger.info("access_log enabled (per-connection dispatch logging on)")

    # drain 阈值：默认 64KB，跨境高 BDP 可调到 256KB~1MB
    drain_thr = int(cfg.get("drain_threshold", 64 * 1024))
    if drain_thr != 64 * 1024:
        set_drain_threshold(drain_thr)
        logger.info("drain_threshold set to %d bytes", drain_thr)

    # 反 DoS 三项
    _IDLE_TIMEOUT_SEC = float(cfg.get("idle_timeout_sec", 1800))
    _MAX_CONNS_PER_IP = int(cfg.get("max_conns_per_ip", 100))
    _TCP_KEEPALIVE    = bool(cfg.get("tcp_keepalive", True))
    logger.info("anti-DoS: idle_timeout=%.0fs, max_conns_per_ip=%d, tcp_keepalive=%s",
                _IDLE_TIMEOUT_SEC, _MAX_CONNS_PER_IP, _TCP_KEEPALIVE)

    # 服务启动时预热握手缓存（只需一次网络请求）
    cache        = HandshakeCache(cfg["camouflage_host"], cfg.get("camouflage_port", 443))
    replay_cache = TokenReplayCache()   # 防重放 nonce 缓存，全局单例
    store        = StatsStore()
    if not await cache.warmup():
        logger.warning("Handshake cache warmup failed — probe connections will be dropped silently")

    # ── egress 配置 + server 端路由（按 target 选 egress）──
    egresses = {"DIRECT": DefaultEgress()}
    for eg in cfg.get("egresses", []):
        name = eg.get("name", "").strip()
        mark = int(eg.get("mark", 0))
        if not name or not mark:
            logger.warning("egress 跳过非法条目: %s", eg)
            continue
        if name == "DIRECT":
            logger.warning("egress 名 'DIRECT' 是保留字，跳过")
            continue
        # 启动期一次性探测 SO_MARK 是否可用，失败 fail loud
        ok, msg = MarkedEgress.probe(mark)
        if not ok:
            logger.error("Egress %s 不可用：%s", name, msg)
            logger.error("提示：用 root 跑，或给二进制 setcap 'cap_net_admin=ep' $(which python3)")
            sys.exit(1)
        egresses[name] = MarkedEgress(name, mark)
        logger.info("Egress configured: %s (%s)", name, msg)

    egress_router = None
    if cfg.get("egress_rules"):
        if len(egresses) <= 1:
            logger.warning("配 egress_rules 但没配 egresses，规则将无效果")
        site_paths, ip_paths = await geo_ensure_all(cfg)
        egress_router = build_router(
            cfg, site_paths, ip_paths,
            valid_actions=set(egresses.keys()),
            rules_field="egress_rules",
        )

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, cfg, cache, replay_cache, store,
                                   egresses, egress_router),
        cfg["listen_host"],
        cfg["listen_port"],
        limit=262144,
    )

    # 在监听 socket 上预设 brutal 算法名称。
    # accept() 产生的子连接会自动继承算法名，之后只需对每条连接单独 set_rate()。
    if cfg.get("brutal_rate_bps", 0):
        for sock in server.sockets:
            if brutal.set_algorithm(sock):
                logger.info("TCP Brutal algorithm pre-set on listening socket")
                break

    logger.info(
        "Listening on %s:%d | camouflage -> %s | cache ready: %s",
        cfg["listen_host"],
        cfg["listen_port"],
        cfg["camouflage_host"],
        cache.ready,
    )

    admin_port = cfg.get("admin_port", 0)
    admin_server = None
    if admin_port:
        admin_host  = cfg.get("admin_host", "127.0.0.1")
        admin_token = cfg.get("admin_token", "")
        admin_server = await start_admin(store, admin_host, admin_port, admin_token)
        token_hint = f"?token={admin_token}" if admin_token else ""
        logger.info("Admin panel: http://%s:%d/%s", admin_host, admin_port, token_hint)

    try:
        if admin_server:
            async with server, admin_server:
                await asyncio.gather(server.serve_forever(),
                                     admin_server.serve_forever())
        else:
            async with server:
                await server.serve_forever()
    finally:
        await time_sync.stop()


if __name__ == "__main__":
    install_uvloop_if_available()

    config = sys.argv[1] if len(sys.argv) > 1 else "config_server.json"
    asyncio.run(main(config))
