"""
时钟偏移同步：避免 client / server 时钟漂移 > 60s（TokenReplayCache 容差）
导致 ClientHello token 全部被服务端误判超时。

策略（按优先级）：
  1. UDP NTP（port 123）   —— 默认路径，毫秒级精度，大多数环境可用
  2. HTTPS Date 头（TCP 443）—— UDP 被墙时兜底，秒级精度（IMF-fixdate）
  3. 均失败 → 保留当前 offset、后台周期重试，不阻塞业务

关键设计：
  - **不动系统时钟**，只在进程内通过 hello_auth 注入的 time provider 应用偏移
  - 多源 median 抗单源时间劫持
  - max_offset_sec 净化：>1 天的偏移视为劫持/误算、直接拒绝
  - HTTPS Date fallback **直连** target，不走代理隧道（避免"隧道又依赖时钟"
    的鸡蛋问题）
  - 启动期阻塞首次同步（≤5s），失败则继续走系统时钟、后台重试

精度对照：
  60s 容差 vs 1s HTTPS Date 精度，余量充裕。
"""

from __future__ import annotations

import asyncio
import ssl as _ssl
import struct
import time
from email.utils import parsedate_to_datetime
from typing import Optional

from .utils import get_logger

logger = get_logger("time_sync")

# NTP 1900-01-01 纪元 → Unix 1970-01-01 纪元 的秒差
_NTP_EPOCH_OFFSET = 2_208_988_800

_DEFAULT_UDP_SERVERS = [
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.google.com",
]
_DEFAULT_TCP_SERVERS = [
    "www.apple.com",
    "www.cloudflare.com",
    "www.microsoft.com",
]


def _parse_duration(s) -> float:
    """支持 "30s" / "5m" / "1h" / "1d" / 裸数字（秒）"""
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().lower()
    if not s:
        return 0.0
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        try:
            return float(s[:-1]) * mult[s[-1]]
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── 单源查询 ──────────────────────────────────────────────────────────────────

async def _ntp_query_udp(server: str, timeout: float = 3.0) -> Optional[float]:
    """
    向 NTP server 发送 NTPv3 client packet，返回服务端 Transmit Timestamp（Unix 秒）。

    Packet 48 字节：
      byte 0:    LI(2) | VN(3) | Mode(3) = 0b00_011_011 = 0x1b（client mode）
      bytes 40-47: 服务端的 Transmit Timestamp（NTP 1900 纪元，32.32 定点）
    """
    loop = asyncio.get_event_loop()
    pkt = b"\x1b" + b"\x00" * 47

    class _Proto(asyncio.DatagramProtocol):
        def __init__(self):
            self.fut: asyncio.Future = loop.create_future()
        def datagram_received(self, data, _addr):
            if not self.fut.done():
                self.fut.set_result(data)
        def error_received(self, exc):
            if not self.fut.done():
                self.fut.set_exception(exc)

    transport = None
    try:
        transport, proto = await asyncio.wait_for(
            loop.create_datagram_endpoint(_Proto, remote_addr=(server, 123)),
            timeout=timeout,
        )
        transport.sendto(pkt)
        data = await asyncio.wait_for(proto.fut, timeout=timeout)
    except Exception as e:
        logger.debug("NTP UDP %s failed: %s", server, e)
        return None
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    if len(data) < 48:
        return None
    secs, frac = struct.unpack("!II", data[40:48])
    if secs == 0:
        return None
    return (secs - _NTP_EPOCH_OFFSET) + (frac / 2**32)


async def _ntp_query_https(host: str, timeout: float = 5.0) -> Optional[float]:
    """
    `HEAD / HTTP/1.1` → 解析 Date 响应头 → Unix 秒。秒级精度。

    走系统默认 CA，**不走代理隧道**——避免"隧道依赖时钟同步、时钟同步又依赖
    隧道"的鸡蛋问题。HTTPS 在 VPS 端口屏蔽场景里几乎不会被墙。
    """
    ctx = _ssl.create_default_context()
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ctx),
            timeout=timeout,
        )
        req = (
            f"HEAD / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: mirage-time-sync/1.0\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(req)
        await writer.drain()

        buf = bytearray()
        # bytearray.find 原生支持 substring 搜索，不需要 bytes() copy（旧实现
        # 每次迭代复制整个 buffer 一次，HEAD 响应通常 < 4KB 还好，但 16KB 上限
        # 下 worst-case 多次复制 ~MB 级临时对象）
        while buf.find(b"\r\n\r\n") < 0:
            try:
                chunk = await asyncio.wait_for(reader.read(2048), timeout=timeout)
            except Exception:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > 16384:
                break
    except Exception as e:
        logger.debug("HTTPS Date %s failed: %s", host, e)
        return None
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    # 找 Date: 头
    for line in bytes(buf).split(b"\r\n"):
        if line.lower().startswith(b"date:"):
            try:
                ds = line[5:].decode("ascii").strip()
                return parsedate_to_datetime(ds).timestamp()
            except Exception:
                return None
    return None


# ── TimeSync 类 ───────────────────────────────────────────────────────────────

class TimeSync:
    """
    后台时钟偏移维护。

    `corrected_time = system_time + offset`

    类级 `_offset` 共享：调用方（hello_auth 等）通过 `TimeSync.corrected_time`
    或 `TimeSync.get_offset()` 看到的是同一个偏移。
    """

    _offset: float = 0.0
    # Clash API /mirage/timesync 用：最近一次成功同步的来源 + 时刻 + 样本数
    _last_source: str = ""
    _last_sync_at_epoch: float = 0.0
    _last_sample_count: int = 0
    _initialized: bool = False

    def __init__(self, cfg: dict):
        ts_cfg = cfg.get("time_sync")
        if not isinstance(ts_cfg, dict):
            ts_cfg = {}
        self.enabled         = bool(ts_cfg.get("enabled", True))
        self.udp_servers     = ts_cfg.get("udp_servers") or _DEFAULT_UDP_SERVERS
        self.tcp_servers     = ts_cfg.get("tcp_servers") or _DEFAULT_TCP_SERVERS
        self.interval        = _parse_duration(ts_cfg.get("interval",        "1h"))
        self.startup_timeout = _parse_duration(ts_cfg.get("startup_timeout", "5s"))
        self.max_offset_sec  = float(ts_cfg.get("max_offset_sec", 86400))  # 1 day
        self._task: Optional[asyncio.Task] = None

    # ── 类级访问点（给 hello_auth 注入用）─────────────────────────────────
    @classmethod
    def get_offset(cls) -> float:
        return cls._offset

    @classmethod
    def corrected_time(cls) -> float:
        return time.time() + cls._offset

    # ── 生命周期 ───────────────────────────────────────────────────────────
    async def initial_sync(self) -> bool:
        """启动期阻塞首次同步（≤ startup_timeout）。失败不挂掉业务。"""
        if not self.enabled:
            logger.info("time_sync disabled by config")
            return False
        try:
            return await asyncio.wait_for(self._sync_once(),
                                          timeout=self.startup_timeout)
        except asyncio.TimeoutError:
            logger.warning("time_sync initial sync timed out (%.1fs); using system "
                           "time, will retry in background", self.startup_timeout)
            return False

    def start(self) -> None:
        """启动后台周期重新同步（每 interval 秒）"""
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("time_sync background loop started (interval %.0fs)", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ── 内部 ────────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("time_sync iteration failed: %s", e)

    async def _sync_once(self) -> bool:
        """从多源采样，取 median 决定新 offset。返回是否成功"""
        samples: list[float] = []
        used_source = "ntp"

        # 1) UDP NTP 优先
        for srv in self.udp_servers:
            t = await _ntp_query_udp(srv)
            if t is not None:
                samples.append(t)
            if len(samples) >= 3:
                break

        # 2) UDP 全黑（防火墙拦 123）→ HTTPS Date 兜底
        if not samples:
            logger.info("UDP NTP unreachable, falling back to HTTPS Date")
            used_source = "https"
            for srv in self.tcp_servers:
                t = await _ntp_query_https(srv)
                if t is not None:
                    samples.append(t)
                if len(samples) >= 3:
                    break

        if not samples:
            logger.warning("all time sync sources failed; keeping offset %.2fs",
                           TimeSync._offset)
            return False

        samples.sort()
        ref = samples[len(samples) // 2]
        local = time.time()
        new_offset = ref - local

        # 净化：>1 天偏移视为劫持/解析错
        if abs(new_offset) > self.max_offset_sec:
            logger.warning("computed offset %.0fs > max %.0fs, rejected "
                           "(possible time hijack)",
                           new_offset, self.max_offset_sec)
            return False

        old = TimeSync._offset
        TimeSync._offset = new_offset
        TimeSync._initialized = True
        TimeSync._last_source = used_source
        TimeSync._last_sync_at_epoch = time.time()
        TimeSync._last_sample_count = len(samples)
        logger.info("time_sync: offset %.2fs → %.2fs (Δ%+.2fs, %d source%s)",
                    old, new_offset, new_offset - old, len(samples),
                    "" if len(samples) == 1 else "s")
        return True
