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

import sys


from core.camouflage import server_read_hello_and_decide

from core.handshake_cache import HandshakeCache

from core.hello_auth import TokenReplayCache

from core.tunnel import EncryptedTunnel

from core.utils import get_logger, unpack_address

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
        client_writer.close()
        return

    logger.info("New connection from %s", peer)

    # 阶段1：读 ClientHello，认证决策 + 握手模拟（含重放检测）
    ok, client_random = await server_read_hello_and_decide(
        client_reader, client_writer, cfg["password"], cache, replay_cache
    )
    if not ok:
        return  # 探测连接已由 camouflage 模块处理完毕

    # 合法客户端：对该 TCP 连接设置 Brutal 速率
    # 算法名称（brutal）已在监听 socket 上预设，accept() 时自动继承，
    # 这里只需设置 per-connection 速率参数。
    rate_bps = cfg.get("brutal_rate_bps", 0)
    if rate_bps:
        sock = client_writer.get_extra_info("socket")
        if sock:
            brutal.set_rate(sock, rate_bps)

    # 阶段2：派生会话密钥（client_random 已从 ClientHello 提取，无需额外握手）
    tunnel = EncryptedTunnel(client_reader, client_writer, cfg["password"])
    await tunnel.do_handshake_as_responder(client_random)

    # 阶段3：读取目标地址
    try:
        # 不设超时：连接池里的空闲连接会在这里等待，直到 SOCKS5
        # 请求到来并发送目标地址，或者 TCP 连接断开（触发异常）。
        addr_packet = await tunnel.recv()
    except Exception:
        logger.debug("Connection from %s closed before sending target address", peer)
        client_writer.close()
        return

    target_host, target_port, _ = unpack_address(addr_packet)
    conn = store.register(client_ip, target_host, target_port, client_writer)
    logger.info("Proxy %s → %s:%d [id=%d]", peer, target_host, target_port, conn.id)

    # 阶段4：连接目标，双向中继
    try:
        target_reader, target_writer = await asyncio.open_connection(target_host, target_port, limit=262144)
    except Exception as e:
        logger.error("Cannot connect to %s:%d: %s", target_host, target_port, e)
        client_writer.close()
        store.unregister(conn)
        return

    async def tunnel_to_target():
        try:
            while True:
                data = await tunnel.recv()
                conn.bytes_up += len(data)
                target_writer.write(data)
                if target_writer.transport.get_write_buffer_size() > 65536:
                    await target_writer.drain()
        except Exception:
            pass
        finally:
            try:
                target_writer.close()
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
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass

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

    logger.info("Connection from %s closed [id=%d]", peer, conn.id)


def _raise_fd_limit() -> None:
    """尝试将进程的文件描述符上限提升至系统硬限制"""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            logger.info("File descriptor limit raised: %d → %d", soft, hard)
    except Exception as e:
        logger.warning("Could not raise fd limit: %s", e)


async def main(config_path: str) -> None:
    _raise_fd_limit()

    with open(config_path) as f:
        cfg = json.load(f)

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
        "Listening on %s:%d | camouflage → %s | cache ready: %s",
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

    servers = [server] + ([admin_server] if admin_server else [])
    async with asyncio.TaskGroup() as tg:
        for s in servers:
            tg.create_task(s.serve_forever())


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        print("[*] uvloop enabled")
    except ImportError:
        pass

    config = sys.argv[1] if len(sys.argv) > 1 else "config_server.json"
    asyncio.run(main(config))
