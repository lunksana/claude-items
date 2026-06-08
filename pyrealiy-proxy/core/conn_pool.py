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

配置参数（node_cfg 字段，由 outbound 层填入）：
  server_host / server_port  服务端地址
  password / camouflage_host 鉴权 + SNI
  brutal_rate_bps            每条连接的 Brutal 速率（建议 5~10 Mbps，0=关）
  brutal_pool_size           预建连接数（建议 10~20，影响最大并发吞吐）

回调：
  on_latency(ms): 每次 build_one 成功后通知调用方实际握手耗时
                  （outbound 层用作 urltest 排序数据）
  on_failure():   build_one 失败一次（连续失败 N 次后 outbound 标 unhealthy）
"""


from __future__ import annotations

import asyncio
import random
import socket
import time
from typing import Callable, Optional


from .hello_auth import make_session_token

from .tls_raw import build_client_hello, build_fake_client_tail

from .tunnel import EncryptedTunnel

from .utils import get_logger

from . import brutal

logger = get_logger("conn_pool")

_BUILD_TIMEOUT = 20.0   # 单条连接建立（TCP+认证+握手模拟）的总超时秒数
_MAX_IDLE_SEC  = 120    # 池中空闲连接最长存活时间（超过后丢弃重建，避免长期无流量被识别）

# 反指纹 jitter：避免"启动时 N 条 TLS 1.3 在 1s 内同 IP 出"这种代理特征
#
# 阶梯延迟：相邻两条 build 的真实启动时间间隔 ≥ _STAGGER_STEP ± _STAGGER_JITTER。
#   该约束由 **池级游标 `_next_build_at`** 维护、**跨 _schedule_refills 调用累计**。
#
# ── 老实现的 bug（0.4.5 之前）─────────────────────────────────────────────
# 老 `_staggered_delay(i)` 每次 _schedule_refills 都从 i=0 重新算阶梯。如果 N
# 个 SOCKS5 请求几乎同时打过来、各自调一次 _schedule_refills 且都看到 deficit=1，
# 每个调用都把唯一那条 build 排到 delay=0 立刻发 —— N 条 SYN 同秒一齐出去。
# 抓包（0604.pcap）显示 19% 的 SYN 相邻间隔 <50ms，属代理批量建连指纹。
#
# 新实现把游标做成池属性 `_next_build_at`（monotonic time），每次新排一条
# build 把它推进 `step ± jitter`，跨调用累计 → 全局任意两条 build 的发起
# 时间间隔 >= 0.22-0.38s。
_STAGGER_STEP   = 0.30
_STAGGER_JITTER = 0.08
_IDLE_JITTER    = 30.0      # _MAX_IDLE_SEC ± 该值随机化每条 tunnel 的实际寿命


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """
    在已建好的 TCP 连接上启用 SO_KEEPALIVE + Linux 调优后的探测周期。

    Linux 默认 TCP_KEEPIDLE=7200s（2 小时静默才探测），对代理场景太宽。
    调到 60s 静默 + 10s 间隔 × 3 次 = 90s 内能确认对端是否活着，让 OS 提前
    把死连接 RST 掉，避免我们的 _ReadyTunnel.is_alive 漏检（虽然有 MSG_PEEK
    兜底，但 OS 主动探测能让池更早 refill）。

    其它平台只开 SO_KEEPALIVE（用 OS 默认探测周期）。
    """
    try:
        sock = writer.transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except Exception as e:
        logger.debug("Cannot enable TCP keepalive: %s", e)


async def _read_server_handshake(reader: asyncio.StreamReader) -> None:
    """
    读取服务端 TLS 1.3 握手 flight 直至结束，并做基本合法性检查。

    ── 成功路径 ──────────────────────────────────────────────────────────────
      ServerHello(0x16) → CCS(0x14) → N×ApplicationData(0x17, 加密记录群)
      CCS 之后切换为短超时（1.5s）；超时即代表 flight 发完。

    ── 失败显式抛 IOError（不再被外层 `pass` 吃掉）──────────────────────────
      - 0x15 Alert：服务端 / 中间设备明文告警（密码错、SNI 拒绝、重放命中等）
      - EOF（IncompleteReadError）：握手中途被对端关连接
      - 12s 内连 CCS 都没收到：服务端响应停滞
      - 未见 SH / CCS / 任一 0x17：flight 不完整

      在握手期就报错的好处：调用方 `_build_one` 直接判 build 失败，错误信息
      精准（如 "server sent TLS alert level=2 desc=51"），不会再走到派生密钥
      和返回 ready_tunnel 那一步、让首次 send/recv 才以"EOF"形式爆出深栈。

    ── 这个检查覆盖不到的事（限制声明）──────────────────────────────────────
      服务端 token 验证失败时会走 camouflage 模式回放真实 apple.com 的握手 ——
      与"接受"模式回放的字节流**完全相同**。客户端无法从握手 flight 判定
      服务端到底是哪种模式。这是协议设计上的隐蔽性目标，本函数不破坏它。
    """
    saw_sh = False
    saw_ccs = False
    saw_enc = False

    while True:
        timeout = 1.5 if saw_ccs else 12.0
        try:
            header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
        except asyncio.TimeoutError:
            if not saw_ccs:
                # 12s 内服务端连 ServerHello+CCS 都没发完
                raise IOError("server TLS handshake stalled (no CCS within 12s)")
            break  # CCS 之后的短超时 = 服务端 flight 发完，正常结束
        except asyncio.IncompleteReadError as e:
            raise IOError(
                f"server closed during TLS handshake "
                f"(read {len(e.partial)} of {e.expected} bytes header)"
            )

        ct     = header[0]
        length = int.from_bytes(header[3:5], "big")
        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        except asyncio.IncompleteReadError as e:
            raise IOError(
                f"server closed during TLS record body "
                f"(ct=0x{ct:02x}, got {len(e.partial)}/{e.expected})"
            )

        if ct == 0x15:
            # Alert 在 TLS 1.2 / 1.3 握手前阶段都是明文：[level, description]
            # 见 RFC 5246 §7.2 / RFC 8446 §6
            lvl  = body[0] if body else 0
            desc = body[1] if len(body) >= 2 else 0
            raise IOError(f"server sent TLS alert (level={lvl}, desc={desc})")
        if ct == 0x16:
            saw_sh = True
        elif ct == 0x14:
            saw_ccs = True
        elif ct == 0x17:
            saw_enc = True
        # 其它 ct 不打断（保留 forward-compat），但也不计入"flight 完成"判定

    if not (saw_sh and saw_ccs and saw_enc):
        raise IOError(
            f"incomplete TLS handshake flight "
            f"(ServerHello={saw_sh}, CCS={saw_ccs}, EncryptedRecords={saw_enc})"
        )


class _ReadyTunnel:
    __slots__ = ("tunnel", "writer", "_created_at", "_lifetime")

    def __init__(self, tunnel: EncryptedTunnel, writer: asyncio.StreamWriter):
        self.tunnel     = tunnel
        self.writer     = writer
        self._created_at = time.monotonic()
        # 每条 tunnel 单独 jitter，避免一批 tunnel 同时过期"齐刷刷重建"的特征
        self._lifetime  = _MAX_IDLE_SEC + random.uniform(-_IDLE_JITTER, _IDLE_JITTER)

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._created_at > self._lifetime

    @property
    def is_alive(self) -> bool:
        """
        实时检查对端是否还在（防"池里有僵尸 tunnel"问题）。

        ── 为什么需要 ─────────────────────────────────────────────────────────
        is_stale 只看时间没看 TCP 真实状态。对端可能因为以下原因悄悄关了但
        我们没感知：服务端 idle timeout / NAT 表项过期 / 网络抖动后 FIN 丢
        包但 RST 也没回 / 服务端崩了 OS 直接回 RST 但我们没读到。第一次
        用户从池里取这条 tunnel、send 才发现失败，用户感受到一次延迟翻倍。

        ── 怎么做 ─────────────────────────────────────────────────────────────
        从池里取出来后立刻 MSG_PEEK 看一字节：
          - BlockingIOError    → 接收缓冲空，对端没事干，**alive**
          - 返回 b''           → 对端已发 FIN，**dead**
          - 返回非空            → 对端意外推了数据（异常状态），**dead** 谨慎丢弃
          - ConnectionResetError / OSError → RST 等，**dead**

        操作是非阻塞 syscall（peek 不消耗数据），asyncio 单线程下 O(1) 微秒级。
        """
        transport = self.writer.transport
        sock = transport.get_extra_info("socket")
        if sock is None:
            return True  # 拿不到底层 socket，保守认为活
        try:
            data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            if data == b"":
                return False  # 对端 FIN
            # 收到非空 = 对端在推数据，但我们的协议下这时不该有 → 异常状态
            return False
        except BlockingIOError:
            return True  # 缓冲空 = 正常空闲
        except (ConnectionResetError, OSError):
            return False
        except Exception:
            return True  # 未知错误保守认为活，让后续 send 报错

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


class BrutalPool:
    """
    维护 pool_size 条到服务端的预认证加密隧道。
    每条隧道独立启用 TCP Brutal（per-connection rate）。

    cfg 是 node_cfg（per-outbound 的连接参数子集），不再读全局 cfg：
    多 outbound 场景下每个 PyrealiyOutbound 独占一个 BrutalPool。
    """

    def __init__(
        self,
        cfg: dict,
        on_latency: Optional[Callable[[float], None]] = None,
        on_failure: Optional[Callable[[], None]] = None,
    ):
        self._cfg       = cfg
        self._pool_size = cfg.get("brutal_pool_size", 10)
        self._rate_bps  = cfg.get("brutal_rate_bps", 0)
        self._queue: asyncio.Queue[_ReadyTunnel] = asyncio.Queue()
        # 正在建立中的连接数。asyncio 单线程，在任意两个 await 之间修改是原子的，
        # 因此可以用普通 int 代替 Lock 来防止并发超建。
        self._building  = 0
        self._on_latency = on_latency
        self._on_failure = on_failure
        # 池级累计阶梯起点（monotonic time），跨 _schedule_refills 调用累计。
        # 见模块顶部注释；用法：_reserve_build_slot() 取一个 delay 并把游标推进
        self._next_build_at = 0.0

    def _reserve_build_slot(self) -> float:
        """
        从池级游标 _next_build_at 预定一个 build 时间槽，返回此槽相对 now 的 delay。

        - 若游标在过去：从 now 开始新一段阶梯（避免长时间空闲后第一条等过去时间）
        - 否则：延续之前的累计阶梯
        - 每次预定后把游标推进 _STAGGER_STEP ± _STAGGER_JITTER

        跨 _schedule_refills 调用累计 → 全局相邻 build 真实间隔 ≥ ~0.22s。
        """
        now = time.monotonic()
        if self._next_build_at < now:
            self._next_build_at = now
        delay = self._next_build_at - now
        self._next_build_at += _STAGGER_STEP + random.uniform(-_STAGGER_JITTER, _STAGGER_JITTER)
        return delay

    async def warmup(self) -> int:
        """
        启动时阶梯预建所有连接。**真阶梯**：相邻 build 真实启动时间间隔
        ≥ ~0.22s（_STAGGER_STEP - _STAGGER_JITTER），由池级游标维护。

        10 条池：第 0 条立即、第 9 条 ~2.7s 后才开始 build。
        总耗时上界 ≈ pool_size × _STAGGER_STEP + 单条 build 时间。
        """
        # warmup 是池生命期起点，游标从 now 重新计起（避免上次会话残留状态）
        self._next_build_at = time.monotonic()
        tasks = [
            self._build_and_enqueue(delay=self._reserve_build_slot())
            for _ in range(self._pool_size)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        logger.info("Pool warmed up: %d/%d connections ready (rate=%.0f Mbps each, staggered)",
                    ok, self._pool_size, self._rate_bps / 1e6)
        return ok

    @property
    def ready_count(self) -> int:
        """当前可用隧道数（队列长度）"""
        return self._queue.qsize()

    async def acquire(self) -> _ReadyTunnel | None:
        """
        取出一条可用隧道，同时触发后台补充。
        - 跳过 is_stale（超过 _MAX_IDLE_SEC）的过期连接
        - 跳过 is_alive=False 的僵尸连接（对端已 FIN/RST 但我们 TCP 没感知）
        若池暂时为空，等待最多 5 秒；超时则直接新建一条（不丢请求）。
        """
        self._schedule_refills()
        try:
            while True:
                ready = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                if ready.is_stale:
                    ready.close()
                    logger.debug("Discarded stale pool connection, rebuilding")
                    self._schedule_refills()
                    continue
                if not ready.is_alive:
                    ready.close()
                    logger.debug("Discarded zombie pool connection (peer FIN/RST), rebuilding")
                    self._schedule_refills()
                    continue
                return ready
        except asyncio.TimeoutError:
            logger.info("Pool exhausted, building direct connection")
            # 即便是 exhausted fallback 也走池级游标：避免多个并发 fallback
            # 同时发 SYN（0605.pcap 残余 8.5% 的同秒爆发就来自这里）。
            # 池空时游标多在过去 → 第 1 个 delay=0 立即发，第 2 个 ~300ms 后。
            delay = self._reserve_build_slot()
            if delay > 0:
                await asyncio.sleep(delay)
            ready, latency_ms = await self._build_one()
            if ready is not None and latency_ms is not None and self._on_latency:
                self._on_latency(latency_ms)
            elif ready is None and self._on_failure:
                self._on_failure()
            return ready

    async def probe_once(self) -> bool:
        """
        主动触发一次握手并把结果塞入池。若池已满则丢弃最老的一条腾位。

        用途：长时间无流量时给 outbound 层补一个新鲜的延迟样本，使 urltest
        决策不至于依赖陈旧数据（被动样本只在有流量时才更新）。
        """
        ready, latency_ms = await self._build_one()
        if ready is None:
            if self._on_failure:
                self._on_failure()
            return False
        if self._on_latency and latency_ms is not None:
            self._on_latency(latency_ms)
        # 池满时挤掉一条最旧的（自然轮换，无锁）
        if self._queue.qsize() >= self._pool_size:
            try:
                old = self._queue.get_nowait()
                old.close()
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(ready)
        return True

    def _schedule_refills(self) -> None:
        """
        计算缺口并按池级游标排队补充 build。

        关键差异（vs 0.4.5 之前）：delay 来自 **池级游标 `_next_build_at`**，
        跨 _schedule_refills 调用累计，所以 N 个并发 acquire 各自调一次本方法
        各排一条 build 时，N 条 build 仍然彼此间隔 _STAGGER_STEP，**而不是**
        每条都从 delay=0 立刻发 → N 条 SYN 同秒一齐出去。
        """
        deficit = self._pool_size - self._queue.qsize() - self._building
        for _ in range(max(0, deficit)):
            asyncio.create_task(self._build_and_enqueue(delay=self._reserve_build_slot()))

    async def _build_and_enqueue(self, delay: float = 0.0) -> bool:
        """
        原子检查 + 占位：在第一个 await 之前先递增 _building，
        保证不会有多余的连接被建立。

        delay > 0 时先 sleep（refill / staggered warmup 用），sleep 期间池可能
        已经被别的 task 填满 —— 原子检查仍能正确拒绝多余建设。
        """
        if delay > 0:
            await asyncio.sleep(delay)
        # 检查与占位之间没有 await，asyncio 单线程保证原子性
        if self._queue.qsize() + self._building >= self._pool_size:
            return False
        self._building += 1
        try:
            ready, latency_ms = await self._build_one()
            if ready:
                if self._on_latency and latency_ms is not None:
                    self._on_latency(latency_ms)
                await self._queue.put(ready)
                return True
            if self._on_failure:
                self._on_failure()
            return False
        finally:
            self._building -= 1

    async def _build_one(self) -> tuple[_ReadyTunnel | None, float | None]:
        """
        建立一条完整的预认证隧道（TCP 连接 + 认证 + 加密握手）。
        返回 (ready, latency_ms)；失败为 (None, None)。

        整体用 _BUILD_TIMEOUT 包裹：任何一步（连接/drain/握手读取）卡住都会
        在硬上限内被取消，避免坏连接长期占用 _building 计数导致池容量空缺。

        latency_ms 是从开始建 TCP 到握手完成的总耗时，作为 outbound 健康
        指标用 —— 比单纯 TCP RTT 更接近实际"取一条隧道用一次"的真实体验。
        """
        t0 = time.monotonic()
        try:
            ready = await asyncio.wait_for(self._build_one_inner(), timeout=_BUILD_TIMEOUT)
            return ready, (time.monotonic() - t0) * 1000.0
        except Exception as e:
            logger.debug("Failed to build pool connection: %s", e)
            return None, None

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

            # 1b. 开 SO_KEEPALIVE：服务端 NAT 表项过期 / VPS 抖动等情况下，
            # OS 内核探测包能在 ~90s 内 RST 死连接，配合 _ReadyTunnel.is_alive
            # 在 acquire 时实时检测，把"池里有僵尸 tunnel"概率降到极小。
            _enable_tcp_keepalive(server_writer)

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
