"""
WS 端点可单测部分：LogBroadcaster + _rss_bytes。

WS handler 本身是无限循环 + 真 socket，留给集成；这里测可隔离的逻辑：
  - B5：LogBroadcaster 不在 handler 层硬过滤（level=DEBUG），交给订阅端 ?level=
  - 广播 subscribe/emit/unsubscribe 机制 + 队列满丢老消息
  - _rss_bytes 返回非负 int（/memory + /connections 的 memory 字段）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api.ws_endpoints import LogBroadcaster, _rss_bytes  # noqa: E402


def _record(level, msg, name="test"):
    return logging.LogRecord(name=name, level=level, pathname="", lineno=0,
                             msg=msg, args=(), exc_info=None)


class TestLogBroadcaster(unittest.TestCase):
    def test_default_level_is_debug(self):
        # B5：构造时不能硬过滤成 INFO，否则用户调 root=DEBUG 也收不到 DEBUG
        bc = LogBroadcaster()
        self.assertEqual(bc.level, logging.DEBUG)

    def test_emit_delivers_to_subscriber(self):
        async def go():
            bc = LogBroadcaster()
            q = bc.subscribe()
            bc.emit(_record(logging.INFO, "hello"))
            msg = q.get_nowait()
            self.assertEqual(msg["type"], "info")
            self.assertIn("hello", msg["payload"])
            self.assertEqual(msg["_levelno"], logging.INFO)
        asyncio.run(go())

    def test_debug_record_delivered(self):
        # handler 不过滤 → DEBUG 也进队列（订阅端再按 ?level= 过滤）
        async def go():
            bc = LogBroadcaster()
            q = bc.subscribe()
            bc.emit(_record(logging.DEBUG, "dbg"))
            self.assertEqual(q.get_nowait()["_levelno"], logging.DEBUG)
        asyncio.run(go())

    def test_unsubscribe_stops_delivery(self):
        async def go():
            bc = LogBroadcaster()
            q = bc.subscribe()
            bc.unsubscribe(q)
            bc.emit(_record(logging.INFO, "x"))
            self.assertTrue(q.empty())
        asyncio.run(go())

    def test_no_subscribers_no_error(self):
        bc = LogBroadcaster()
        bc.emit(_record(logging.INFO, "nobody listening"))  # 不应抛

    def test_full_queue_drops_oldest(self):
        async def go():
            bc = LogBroadcaster()
            q = bc.subscribe()
            # 灌满 + 多 1：队列上限 256
            for i in range(300):
                bc.emit(_record(logging.INFO, f"m{i}"))
            # 没爆，且队列没超上限
            self.assertLessEqual(q.qsize(), 256)
            # 最新的应还在（丢的是老的）
            msgs = []
            while not q.empty():
                msgs.append(q.get_nowait()["payload"])
            self.assertTrue(any("m299" in m for m in msgs))
            self.assertFalse(any("m0 " in m or m.endswith("m0") for m in msgs))
        asyncio.run(go())

    def test_multiple_subscribers_each_get_copy(self):
        async def go():
            bc = LogBroadcaster()
            q1 = bc.subscribe()
            q2 = bc.subscribe()
            bc.emit(_record(logging.WARNING, "broadcast"))
            self.assertIn("broadcast", q1.get_nowait()["payload"])
            self.assertIn("broadcast", q2.get_nowait()["payload"])
        asyncio.run(go())


class TestRssBytes(unittest.TestCase):
    def test_returns_nonneg_int(self):
        v = _rss_bytes()
        self.assertIsInstance(v, int)
        self.assertGreaterEqual(v, 0)

    def test_plausible_on_linux(self):
        # Linux 上当前进程 RSS 应 > 1MB（解释器本身就占这么多）。
        # 非 Linux（无 /proc/self/statm）返回 0，跳过该断言。
        if os.path.exists("/proc/self/statm"):
            self.assertGreater(_rss_bytes(), 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
