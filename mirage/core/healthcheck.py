"""
后台健康检查（被动 + 主动兜底）

被动样本：BrutalPool 每次成功 build 都会回调 on_latency，把握手耗时塞进
对应 MirageOutbound 的滚动样本窗口。warmup / refill / acquire-miss 都
是天然的样本源，所以正常有流量时 urltest 决策始终基于近期数据，本模块
不需要做任何事。

主动兜底：长时间无流量时被动样本不再产生，urltest 可能依赖几小时前的
旧数据做决策。本模块定期扫描所有 mirage outbound，把 last_sample_time
超过 _STALE_AFTER 的触发一次 probe_once（额外 build 一条 + 挤掉最老一
条；既补样本也刷新池）。

频率：默认每 60s 扫一次，单 outbound stale > 300s 才探。
开销：每 5 分钟最多 N 次 build（N = mirage outbound 数量）。
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from .outbound import Outbound, MirageOutbound
from .utils import get_logger

logger = get_logger("healthcheck")

_SCAN_INTERVAL = 60.0    # 每 60s 检查一次（与 urltest 默认 interval 对齐）
_STALE_AFTER   = 300.0   # 5 min 无新样本 → 主动 probe


class HealthCheck:
    """所有 mirage outbound 共用一个扫描 task"""

    def __init__(
        self,
        outbounds: Iterable[Outbound],
        scan_interval: float = _SCAN_INTERVAL,
        stale_after:   float = _STALE_AFTER,
    ):
        # 只关心叶子节点；组节点的健康度来自其 child
        self._targets = [o for o in outbounds if isinstance(o, MirageOutbound)]
        self._scan_interval = scan_interval
        self._stale_after = stale_after
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None and self._targets:
            self._task = asyncio.create_task(self._loop())
            logger.info("Health check started: %d mirage outbound(s), every %.0fs, stale=%.0fs",
                        len(self._targets), self._scan_interval, self._stale_after)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._scan_interval)
                for o in self._targets:
                    if o.latency_age_sec > self._stale_after:
                        # 各自 probe 并行；这里不 await，避免一个慢的拖整轮
                        asyncio.create_task(self._probe_one(o))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("health check iteration failed: %s", e)

    async def _probe_one(self, o: MirageOutbound) -> None:
        try:
            ms = await o.probe()
            if ms is not None:
                logger.debug("[%s] probe ok, median=%.0fms", o.tag, ms)
            else:
                logger.debug("[%s] probe failed", o.tag)
        except Exception as e:
            logger.debug("[%s] probe exception: %s", o.tag, e)
