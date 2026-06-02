"""
PyReality 服务端

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

import resource

import socket

import sys


from core.camouflage import server_read_hello_and_decide

from core.handshake_cache import HandshakeCache

from core.hello_auth import TokenReplayCache

from core.tunnel import EncryptedTunnel

from core.utils import (get_logger, unpack_address, safe_close,
                        install_stale_gaierror_handler, set_drain_threshold,
                        get_drain_threshold)


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


def _set_keepalive(sock: socket.socket) -> None:
    """
    在已 accept 的 TCP socket 上开 SO_KEEPALIVE，并把 Linux 默认的 2 小时探测
    阈值调到 ~90s（60s 静默 + 10s 间隔 × 3 次探测），让"客户端 NAT/路由器/wifi
    断了但没发 FIN"的死连接在 90s 内被踢掉。
    Linux 才有 TCP_KEEPIDLE/INTVL/CNT；其它平台只开 SO_KEEPALIVE。
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError as e:
        logger.debug("Cannot enable TCP keepalive: %s", e)

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
            _set_keepalive(sock)

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

        target_host, target_port, _conn_unused = unpack_address(addr_packet)
        conn = store.register(client_ip, target_host, target_port, client_writer)
        _alog("info", "Proxy %s -> %s:%d [id=%d]", peer, target_host, target_port, conn.id)

        # 阶段4：连接目标，双向中继
        try:
            target_reader, target_writer = await asyncio.open_connection(target_host, target_port, limit=262144)
        except Exception as e:
            logger.error("Cannot connect to %s:%d: %s", target_host, target_port, e)
            await safe_close(client_writer)
            store.unregister(conn)
            return

        async def tunnel_to_target():
            try:
                while True:
                    data = await tunnel.recv()
                    conn.bytes_up += len(data)
                    target_writer.write(data)
                    if target_writer.transport.get_write_buffer_size() > get_drain_threshold():
                        await target_writer.drain()
            except Exception:
                pass
            finally:
                await safe_close(target_writer)

        async def target_to_tunnel():
            try:
                while True:
                    data = await target_reader.read(65536)
                    if not data:
                        break
                    conn.bytes_down += len(data)
                    await tunnel.send(data)
            except Exception:
                pass
            finally:
                # 主动关方：先发 TLS close_notify alert 再 FIN，外观更像真实 HTTPS 关闭
                await tunnel.send_close_notify()
                await safe_close(client_writer)

        task_a = asyncio.create_task(tunnel_to_target())
        task_b = asyncio.create_task(target_to_tunnel())
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
            store.unregister(conn)

        _alog("info", "Connection from %s closed [id=%d]", peer, conn.id)

    finally:
        # 无论怎么走（正常返回 / 异常 / DoS 兜底 timeout），都释放 IP 槽
        _release_ip_slot(client_ip)


def _raise_fd_limit() -> None:
    """尝试将进程的文件描述符上限提升至系统硬限制"""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            logger.info("File descriptor limit raised: %d -> %d", soft, hard)
    except Exception as e:
        logger.warning("Could not raise fd limit: %s", e)


async def main(config_path: str) -> None:
    _raise_fd_limit()

    # 静音 stale getaddrinfo future 的噪音（详见 utils.install_stale_gaierror_handler）
    install_stale_gaierror_handler(asyncio.get_running_loop())

    with open(config_path) as f:
        cfg = json.load(f)

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

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, cfg, cache, replay_cache, store),
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

    if admin_server:
        async with server, admin_server:
            await asyncio.gather(server.serve_forever(),
                                 admin_server.serve_forever())
    else:
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        print("[*] uvloop enabled")
    except ImportError:
        pass

    config = sys.argv[1] if len(sys.argv) > 1 else "config_server.json"
    asyncio.run(main(config))
