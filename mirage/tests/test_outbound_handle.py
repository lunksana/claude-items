"""
Outbound.handle() 签名 + 转发回归测试

历史 bug：_GroupBase 和 BlockOutbound 的 handle() 漏了 on_up / on_down 形参，
而 client._dispatch 无条件传入这两个 kwargs → 任何走 group / block 的流量
TypeError，连接默默断开。用户症状是"无法 direct"：default 路由经 group 时
全部异常断开。

覆盖：
  1. 所有 Outbound 子类的 handle() 形参必须接受 on_up / on_down（防止漏）
  2. _GroupBase.handle 必须把 on_up / on_down 真的转发给 leaf（不能只接受不传）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.outbound import (  # noqa: E402
    DirectOutbound, BlockOutbound, MirageOutbound, Outbound,
)
from core.group import (  # noqa: E402
    _GroupBase, UrlTestGroup, FallbackGroup, SelectorGroup,
)


class _StubLeaf(Outbound):
    """记录 handle 被调时收到的 on_up / on_down"""
    type = "stub"

    def __init__(self, tag: str = "leaf"):
        self.tag = tag
        self.calls: list[tuple] = []

    async def handle(self, local_reader, local_writer, target_host, target_port,
                     on_up=None, on_down=None):
        self.calls.append((on_up, on_down))

    @property
    def is_healthy(self) -> bool:
        return True


class _NullWriter:
    """StreamWriter 最小桩，让 safe_close 路径不炸"""
    def get_extra_info(self, _):
        return ("127.0.0.1", 0)
    def is_closing(self):
        return False
    def close(self):
        pass
    async def wait_closed(self):
        pass
    def can_write_eof(self):
        return False
    def write_eof(self):
        pass
    def write(self, _):
        pass
    async def drain(self):
        pass


def _up_cb(_n: int) -> None:
    pass


def _down_cb(_n: int) -> None:
    pass


class TestOutboundHandleSignature(unittest.TestCase):
    """
    所有 Outbound 子类的 handle() 都必须接受 on_up / on_down kwargs。
    client._dispatch 无条件传这两个 kwargs；漏一个就是 TypeError。
    """
    CLASSES = (
        DirectOutbound, BlockOutbound, MirageOutbound,
        _GroupBase, UrlTestGroup, FallbackGroup, SelectorGroup,
    )

    def test_all_subclasses_accept_on_up_on_down(self):
        for cls in self.CLASSES:
            with self.subTest(cls=cls.__name__):
                params = inspect.signature(cls.handle).parameters
                self.assertIn("on_up", params,
                              f"{cls.__name__}.handle() missing 'on_up' kwarg")
                self.assertIn("on_down", params,
                              f"{cls.__name__}.handle() missing 'on_down' kwarg")


class TestGroupHandleForwards(unittest.TestCase):
    """
    _GroupBase.handle 必须把 on_up / on_down 真的转发到 leaf.handle()。
    只接受不转发 = 静默丢失 stats 回调，比 TypeError 更难发现。
    """
    def _run(self, group, leaf):
        async def go():
            await group.handle(None, _NullWriter(), "example.com", 443,
                               on_up=_up_cb, on_down=_down_cb)
        asyncio.run(go())
        self.assertEqual(len(leaf.calls), 1, "leaf.handle was not called")
        recv_up, recv_down = leaf.calls[0]
        self.assertIs(recv_up,   _up_cb,   "leaf did not receive on_up")
        self.assertIs(recv_down, _down_cb, "leaf did not receive on_down")

    def test_selector_forwards(self):
        leaf = _StubLeaf("a")
        self._run(SelectorGroup("pick", [leaf], default="a"), leaf)

    def test_urltest_forwards(self):
        leaf = _StubLeaf("a")
        self._run(UrlTestGroup("auto", [leaf]), leaf)

    def test_fallback_forwards(self):
        leaf = _StubLeaf("a")
        self._run(FallbackGroup("fb", [leaf]), leaf)


class TestBlockHandleAcceptsCallbacks(unittest.TestCase):
    """BlockOutbound 没有 leaf 转发，但仍必须接受 kwargs 不抛"""
    def test_block_handle_no_typeerror(self):
        async def go():
            b = BlockOutbound("block")
            await b.handle(None, _NullWriter(), "ad.example.com", 80,
                           on_up=_up_cb, on_down=_down_cb)
        asyncio.run(go())   # 不抛即通过


if __name__ == "__main__":
    unittest.main(verbosity=2)
