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
import time


from .hello_auth import make_session_token

from .tls_raw import build_client_hello, build_fake_client_tail

from .tunnel import EncryptedTunnel

from .utils import get_logger

from . import brutal

logger = get_logger("conn_pool")

_BUILD_TIMEOUT = 20.0   # 单条连接建立（TCP+认证+握手模拟）的总超时秒数
_MAX_IDLE_SEC  = 120    # 池中空闲连接最长存活时间（超过后丢弃重建，避免长期无流量被识别）


async def _read_server_handshake(reader: asyncio.StreamReader) -> None:
    """
    读取服务端 TLS 1.3 握手 flight 直至结束。

    序列：ServerHello(0x16) → CCS(0x14) → N×ApplicationData(0x17, 加密记录群)
    CCS 之后切换为短超时（1.5s）；超时意味着服务端 flight 已发完，无需继续等待。
    """
    saw_ccs = False
    try:
        while True:
            timeout = 1.5 if saw_ccs else 12.0
            try:
                header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
            except asyncio.TimeoutError:
                break  # server flight done
            ct     = header[0]
            length = int.from_bytes(header[3:5], "big")
            await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
            if ct == 0x14:
                saw_ccs = True
    except Exception:
        pass


class _ReadyTunnel:
    __slots__ = ("tunnel", "writer", "_created_at")

    def __init__(self, tunnel: EncryptedTunnel, writer: asyncio.StreamWriter):
        self.tunnel     = tunnel
        self.writer     = writer
        self._created_at = time.monotonic()

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._created_at > _MAX_IDLE_SEC

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
        跳过超过 _MAX_IDLE_SEC 的过期连接（关闭并触发补充）。
        若池暂时为空，等待最多 5 秒；超时则直接新建一条（不丢请求）。
        """
        self._schedule_refills()
        try:
            while True:
                ready = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                if not ready.is_stale:
                    return ready
                ready.close()
                logger.debug("Discarded stale pool connection, rebuilding")
                self._schedule_refills()
        except asyncio.TimeoutError:
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
        """
        建立一条完整的预认证隧道（TCP 连接 + 认证 + 加密握手）。

        整体用 _BUILD_TIMEOUT 包裹：任何一步（连接/drain/握手读取）卡住都会
        在硬上限内被取消，避免坏连接长期占用 _building 计数导致池容量空缺。
        """
        try:
            return await asyncio.wait_for(self._build_one_inner(), timeout=_BUILD_TIMEOUT)
        except Exception as e:
            logger.debug("Failed to build pool connection: %s", e)
            return None

    async def _build_one_inner(self) -> _ReadyTunnel:
        cfg = self._cfg
        server_writer: asyncio.StreamWriter | None = None
        try:
            # 1. 建立 TCP 连接（外层已有总超时，这里不再单独 wait_for）
            if self._rate_bps:
                server_reader, server_writer = await brutal.open_brutal_connection(
                    cfg["server_host"], cfg["server_port"], self._rate_bps
                )
            else:
                server_reader, server_writer = await asyncio.open_connection(
                    cfg["server_host"], cfg["server_port"], limit=262144
                )

            # 2. 发送含认证 token 的 ClientHello，提取 client_random 用于密钥派生
            token = make_session_token(cfg["password"])
            hello, client_random = build_client_hello(cfg["camouflage_host"], token)
            server_writer.write(hello)
            await server_writer.drain()

            # 3. 读取服务端 TLS 1.3 握手 flight（ServerHello + CCS + 加密记录群）
            await _read_server_handshake(server_reader)

            # 4. 发送假 CCS + Finished，完成 TLS 1.3 握手模拟
            server_writer.write(build_fake_client_tail())
            await server_writer.drain()

            # 5. 派生会话密钥，无需额外传输 salt
            tunnel = EncryptedTunnel(server_reader, server_writer, cfg["password"])
            await tunnel.do_handshake_as_initiator(client_random)

            return _ReadyTunnel(tunnel, server_writer)

        except BaseException:
            # 用 BaseException 捕获 wait_for 触发的 CancelledError，保证 fd 释放
            if server_writer is not None:
                try:
                    server_writer.close()
                except Exception:
                    pass
            raise
