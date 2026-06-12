"""
WS 端点：/traffic（1Hz 速率）+ /logs（实时日志）。

handler 签名（router.add_ws 注册的）：
    async def handler(req, ctx, reader, writer, **path_params) → None

握手由 server._maybe_dispatch_ws 完成；handler 拿到 reader/writer 后包成
WSConnection 即可发送 text 帧。WSConnection 自动处理对端 ping/close。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .http_proto import Request
from .router import Router
from .server import APIContext
from .ws_proto import WSConnection

_TRAFFIC_INTERVAL_SEC = 1.0
_LOG_QUEUE_MAX = 256


def register(router: Router, ctx: APIContext) -> None:
    router.add_ws("/traffic", _traffic)
    router.add_ws("/logs",    _logs)


async def _traffic(req: Request, ctx: APIContext, reader, writer) -> None:
    """
    Clash 标准 WS：1 Hz 推送 `{"up": int, "down": int}`，单位 bytes/sec。

    速率 = (cur_total − last_total) / dt。每条订阅独立采样，先到的不影响后到的。
    """
    ws = WSConnection(reader, writer)
    if ctx.registry is None:
        await ws.send_text(json.dumps({"up": 0, "down": 0}))
        await ws.close()
        return

    meter = ctx.registry.meter
    last_up = meter.up_total
    last_down = meter.down_total
    try:
        while not ws.closed:
            await asyncio.sleep(_TRAFFIC_INTERVAL_SEC)
            if ws.closed:
                break
            new_up = meter.up_total
            new_down = meter.down_total
            try:
                await ws.send_text(json.dumps({
                    "up":   new_up - last_up,
                    "down": new_down - last_down,
                }))
            except Exception:
                break
            last_up, last_down = new_up, new_down
    finally:
        await ws.close()


async def _logs(req: Request, ctx: APIContext, reader, writer) -> None:
    """
    Clash 标准 WS：每条日志一个 message `{"type": level, "payload": text}`。

    query 参数 `?level=info|warning|error|debug` 过滤（>= 该级别才推）。
    缺省 info。
    """
    ws = WSConnection(reader, writer)
    if ctx.log_broadcaster is None:
        await ws.send_text(json.dumps({"type": "warning", "payload": "log broadcaster not active"}))
        await ws.close()
        return

    level_name = (req.query.get("level") or "info").upper()
    min_level = getattr(logging, level_name, logging.INFO)
    if not isinstance(min_level, int):
        min_level = logging.INFO

    q: asyncio.Queue = ctx.log_broadcaster.subscribe()
    try:
        while not ws.closed:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue  # 周期性醒来看 ws.closed
            if msg["_levelno"] < min_level:
                continue
            try:
                await ws.send_text(json.dumps({
                    "type":    msg["type"],
                    "payload": msg["payload"],
                }))
            except Exception:
                break
    finally:
        ctx.log_broadcaster.unsubscribe(q)
        await ws.close()


# ============================================================
# LogBroadcaster：logging handler → 多订阅广播
# ============================================================

class LogBroadcaster(logging.Handler):
    """
    挂到 root logger 上，emit() 把每条 record 推到所有订阅者的 queue。

    使用：
        bc = LogBroadcaster()
        bc.attach()  # 启动期
        ...
        bc.detach()  # 关闭期

    线程模型：项目全 asyncio 单线程，emit 在 loop 内被调；put_nowait 同步即可。
    队列满（订阅者消费慢）→ drop 老消息保留新的，避免无限堆。
    """

    def __init__(self, level: int = logging.INFO):
        super().__init__(level=level)
        self._queues: list[asyncio.Queue] = []
        self._formatter = logging.Formatter("[%(name)s] %(message)s")
        self.setFormatter(self._formatter)

    def attach(self) -> None:
        logging.getLogger().addHandler(self)

    def detach(self) -> None:
        try:
            logging.getLogger().removeHandler(self)
        except Exception:
            pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_LOG_QUEUE_MAX)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        if not self._queues:
            return
        try:
            text = self.format(record)
        except Exception:
            return
        msg = {
            "type":     record.levelname.lower(),
            "payload":  text,
            "_levelno": record.levelno,    # 给订阅端过滤用
        }
        for q in list(self._queues):
            if q.full():
                # drop 老消息保留新的
                try:
                    q.get_nowait()
                except Exception:
                    pass
            try:
                q.put_nowait(msg)
            except Exception:
                pass
