"""
PyReality 客户端

本地监听 SOCKS5 → 从 Brutal 连接池取一条预建隧道 → 发送目标地址 → 中继数据

Brutal 多连接策略：
  brutal_rate_bps  = 每条连接的速率（建议 5~10 Mbps）
  brutal_pool_size = 预建连接数（建议 10~20）
  总吞吐上限       ≈ brutal_pool_size × brutal_rate_bps

用法：
  python client.py [config_client.json]
"""


from __future__ import annotations

import asyncio

import json

import resource

import sys


from core.socks5 import parse_socks5_request

from core.conn_pool import BrutalPool

from core.tunnel import EncryptedTunnel

from core.utils import get_logger, pack_address

from core import brutal

logger = get_logger("client")


async def handle_local_connection(
    local_reader: asyncio.StreamReader,
    local_writer: asyncio.StreamWriter,
    pool: BrutalPool,
) -> None:
    # 1. 解析 SOCKS5 请求，获取目标地址
    target = await parse_socks5_request(local_reader, local_writer)
    if target is None:
        local_writer.close()
        return
    target_host, target_port = target
    logger.info("SOCKS5 → %s:%d", target_host, target_port)

    # 2. 从池中取一条预认证隧道（通常无需等待，已提前建好）
    ready = await pool.acquire()
    if ready is None:
        logger.error("No available tunnel for %s:%d", target_host, target_port)
        local_writer.close()
        return

    tunnel = ready.tunnel
    server_writer = ready.writer

    # 3. 发送目标地址（此时隧道已认证并完成密钥握手，直接发）
    try:
        await tunnel.send(pack_address(target_host, target_port))
    except Exception as e:
        logger.error("Failed to send target address: %s", e)
        ready.close()
        local_writer.close()
        return

    # 4. 双向中继：本地应用 ↔ 加密隧道
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
            try:
                server_writer.close()
            except Exception:
                pass

    async def tunnel_to_local():
        try:
            while True:
                data = await tunnel.recv()
                local_writer.write(data)
                await local_writer.drain()
        except Exception:
            pass
        finally:
            try:
                local_writer.close()
            except Exception:
                pass

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


def _raise_fd_limit() -> None:
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

    rate_bps  = cfg.get("brutal_rate_bps", 0)
    pool_size = cfg.get("brutal_pool_size", 10)

    # 启动时检测 Brutal 可用性并打印提示
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
            cfg["brutal_rate_bps"] = 0  # 回退，避免连接失败

    # 预建连接池
    pool = BrutalPool(cfg)
    await pool.warmup()

    server = await asyncio.start_server(
        lambda r, w: handle_local_connection(r, w, pool),
        cfg["socks5_host"],
        cfg["socks5_port"],
    )
    logger.info(
        "SOCKS5 listening on %s:%d  (pool: %d conns ready)",
        cfg["socks5_host"],
        cfg["socks5_port"],
        pool_size,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    config = sys.argv[1] if len(sys.argv) > 1 else "config_client.json"
    asyncio.run(main(config))
