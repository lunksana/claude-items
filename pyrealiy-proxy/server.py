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

from core.tunnel import EncryptedTunnel

from core.utils import get_logger, unpack_address

from core import brutal

logger = get_logger("server")


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    cfg: dict,
    cache: HandshakeCache,
) -> None:
    peer = client_writer.get_extra_info("peername")
    logger.info("New connection from %s", peer)

    # 阶段1：读 ClientHello，认证决策
    ok = await server_read_hello_and_decide(
        client_reader, client_writer, cfg["password"], cache
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

    # 阶段2：加密信道握手
    tunnel = EncryptedTunnel(client_reader, client_writer, cfg["password"])
    await tunnel.do_handshake_as_responder()

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
    logger.info("Proxy %s → %s:%d", peer, target_host, target_port)

    # 阶段4：连接目标，双向中继
    try:
        target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
    except Exception as e:
        logger.error("Cannot connect to %s:%d: %s", target_host, target_port, e)
        client_writer.close()
        return

    async def tunnel_to_target():
        try:
            while True:
                data = await tunnel.recv()
                target_writer.write(data)
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

    logger.info("Connection from %s closed", peer)


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

    # 服务启动时预热缓存（只需一次网络请求）
    cache = HandshakeCache(cfg["camouflage_host"], cfg.get("camouflage_port", 443))
    if not await cache.warmup():
        logger.warning("Handshake cache warmup failed — probe connections will be dropped silently")

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, cfg, cache),
        cfg["listen_host"],
        cfg["listen_port"],
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
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    config = sys.argv[1] if len(sys.argv) > 1 else "config_server.json"
    asyncio.run(main(config))
