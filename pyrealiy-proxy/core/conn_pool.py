"""
Brutal 连接预建池

核心思路（来自 Hysteria2 实践）：
  不用多路复用，改为多条独立 TCP 连接，每条设置适中的 Brutal 速率。
  每条连接各自尝试跑满自己的速率，总吞吐 = 连接数 × 单连接速率。

  例：20 条 × 8 Mbps = ~160 Mbps，比单条 160 Mbps 更稳定，
  因为单条高速 Brutal 在拥塞时容易不稳定，小速率多连接更均匀。

预建池的作用：
  普通做法：SOCKS5 请求 → TCP 连接 → 认证握手 → 开始传输（有延迟）
  预建池：   提前建好 N 条已认证隧道 → SOCKS5 请求来了直接取用（无延迟）
             取走一条 → 后台异步补充一条，始终保持池满

配置参数：
  brutal_rate_bps      每条连接的 Brutal 速率（建议 5~10 Mbps）
  brutal_pool_size     预建连接数（建议 10~20，影响最大并发吞吐）
"""


from __future__ import annotations

import asyncio


from .hello_auth import make_session_token

from .tls_raw import build_client_hello

from .tunnel import EncryptedTunnel

from .utils import get_logger

from . import brutal

logger = get_logger("conn_pool")

_BUILD_TIMEOUT = 15.0   # 单条连接建立（TCP+认证+握手）的总超时秒数


class _ReadyTunnel:
    __slots__ = ("tunnel", "writer")

    def __init__(self, tunnel: EncryptedTunnel, writer: asyncio.StreamWriter):
        self.tunnel = tunnel
        self.writer = writer

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


class BrutalPool:
    """
    维护 pool_size 条到服务端的预认证加密隧道。
    每条隧道独立启用 TCP Brutal（per-connection rate）。
    """

    def __init__(self, cfg: dict):
        self._cfg       = cfg
        self._pool_size = cfg.get("brutal_pool_size", 10)
        self._rate_bps  = cfg.get("brutal_rate_bps", 0)
        self._queue: asyncio.Queue[_ReadyTunnel] = asyncio.Queue()
        # 正在建立中的连接数。asyncio 单线程，在任意两个 await 之间修改是原子的，
        # 因此可以用普通 int 代替 Lock 来防止并发超建。
        self._building  = 0

    async def warmup(self) -> None:
        """启动时并发预建所有连接"""
        tasks = [self._build_and_enqueue() for _ in range(self._pool_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        logger.info("Pool warmed up: %d/%d connections ready (rate=%.0f Mbps each)",
                    ok, self._pool_size, self._rate_bps / 1e6)

    async def acquire(self) -> _ReadyTunnel | None:
        """
        取出一条可用隧道，同时触发后台补充。
        若池暂时为空，等待最多 5 秒；超时则直接新建一条（不丢请求）。
        """
        self._schedule_refills()
        try:
            ready = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            return ready
        except asyncio.TimeoutError:
            # 池暂时耗尽（并发请求超过池大小），直接建连不丢请求
            logger.info("Pool exhausted, building direct connection")
            return await self._build_one()

    def _schedule_refills(self) -> None:
        """
        计算缺口并创建对应数量的补充任务，同时运行（允许并发）。
        缺口 = pool_size - (已就绪数 + 正在建立数)
        """
        deficit = self._pool_size - self._queue.qsize() - self._building
        for _ in range(max(0, deficit)):
            asyncio.create_task(self._build_and_enqueue())

    async def _build_and_enqueue(self) -> bool:
        """
        原子检查 + 占位：在第一个 await 之前先递增 _building，
        保证不会有多余的连接被建立。
        """
        # 检查与占位之间没有 await，asyncio 单线程保证原子性
        if self._queue.qsize() + self._building >= self._pool_size:
            return False
        self._building += 1
        try:
            ready = await self._build_one()
            if ready:
                await self._queue.put(ready)
                return True
            return False
        finally:
            self._building -= 1

    async def _build_one(self) -> _ReadyTunnel | None:
        """建立一条完整的预认证隧道（TCP 连接 + 认证 + 加密握手）"""
        cfg = self._cfg
        server_writer = None
        try:
            # 1. 建立 TCP 连接
            if self._rate_bps:
                server_reader, server_writer = await asyncio.wait_for(
                    brutal.open_brutal_connection(
                        cfg["server_host"], cfg["server_port"], self._rate_bps
                    ),
                    timeout=_BUILD_TIMEOUT,
                )
            else:
                server_reader, server_writer = await asyncio.wait_for(
                    asyncio.open_connection(cfg["server_host"], cfg["server_port"]),
                    timeout=_BUILD_TIMEOUT,
                )

            # 2. 发送含认证 token 的 ClientHello
            token = make_session_token(cfg["password"])
            hello = build_client_hello(cfg["camouflage_host"], token)
            server_writer.write(hello)
            await server_writer.drain()

            # 3. 完成加密信道握手
            tunnel = EncryptedTunnel(server_reader, server_writer, cfg["password"])
            await asyncio.wait_for(tunnel.do_handshake_as_initiator(), timeout=8.0)

            return _ReadyTunnel(tunnel, server_writer)

        except Exception as e:
            logger.debug("Failed to build pool connection: %s", e)
            # 无论在哪一步失败，都必须关闭已打开的 writer，释放文件描述符
            if server_writer is not None:
                try:
                    server_writer.close()
                except Exception:
                    pass
            return None
