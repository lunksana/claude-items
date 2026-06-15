"""
SelectorGroup（手动选节点）单元测试。

覆盖：
  - 默认选中（configured default / 首个 child）
  - select 切换合法 child / 拒非法 child
  - is_healthy 反映当前选中（非任一）
  - resolve_leaf 跟随选中；选中失效回退首个
  - build_outbounds 正确构造 selector 类型
  - 嵌套：selector 的 child 可以是另一个组
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.group import SelectorGroup  # noqa: E402


class _Leaf:
    """最小 Outbound 桩。"""
    def __init__(self, tag, healthy=True, latency=None):
        self.tag = tag
        self.type = "mirage"
        self._healthy = healthy
        self.latency_ms = latency
        self.latency_age_sec = 0.0

    @property
    def is_healthy(self):
        return self._healthy

    def resolve_leaf(self):
        return self


class TestSelectorGroup(unittest.TestCase):
    def _grp(self, default=None, healths=None):
        a = _Leaf("a", healthy=(healths or {}).get("a", True))
        b = _Leaf("b", healthy=(healths or {}).get("b", True))
        c = _Leaf("c", healthy=(healths or {}).get("c", True))
        return SelectorGroup("pick", [a, b, c], default=default)

    def test_default_is_first_child(self):
        self.assertEqual(self._grp().selected, "a")

    def test_configured_default(self):
        self.assertEqual(self._grp(default="b").selected, "b")

    def test_invalid_default_falls_back_first(self):
        self.assertEqual(self._grp(default="nonexistent").selected, "a")

    def test_select_valid(self):
        g = self._grp()
        self.assertTrue(g.select("c"))
        self.assertEqual(g.selected, "c")

    def test_select_invalid_rejected(self):
        g = self._grp(default="a")
        self.assertFalse(g.select("zzz"))
        self.assertEqual(g.selected, "a")   # 不变

    def test_resolve_leaf_follows_selection(self):
        g = self._grp()
        g.select("b")
        self.assertEqual(g.resolve_leaf().tag, "b")

    def test_is_healthy_reflects_selected(self):
        # 选中 b 不健康，即使 a/c 健康，组也算不健康（手动选了就认它）
        g = self._grp(healths={"a": True, "b": False, "c": True})
        g.select("b")
        self.assertFalse(g.is_healthy)
        g.select("a")
        self.assertTrue(g.is_healthy)

    def test_resolve_falls_back_if_selected_vanishes(self):
        # 直接破坏内部状态模拟 selected 失效（reload 删了 child）
        g = self._grp()
        g._selected = "ghost"
        self.assertEqual(g.resolve_leaf().tag, "a")   # 回退首个


class TestSelectorBuildOutbounds(unittest.TestCase):
    def _cfg(self):
        return {"outbounds": [
            {"tag": "jp", "type": "mirage", "server": "1.1.1.1", "server_port": 443,
             "password": "x", "sni": "a.com"},
            {"tag": "us", "type": "mirage", "server": "2.2.2.2", "server_port": 443,
             "password": "x", "sni": "a.com"},
            {"tag": "pick", "type": "selector", "outbounds": ["jp", "us"], "default": "us"},
            {"tag": "direct", "type": "direct"},
            {"tag": "block", "type": "block"},
        ]}

    def test_build_selector(self):
        from core.outbound import build_outbounds
        obs = build_outbounds(self._cfg())
        sel = obs["pick"]
        self.assertEqual(sel.type, "selector")
        self.assertEqual(sel.selected, "us")
        self.assertTrue(sel.select("jp"))
        self.assertEqual(sel.resolve_leaf().tag, "jp")

    def test_selector_nesting(self):
        # selector 的 child 是 urltest 组
        from core.outbound import build_outbounds
        cfg = self._cfg()
        cfg["outbounds"].append(
            {"tag": "auto", "type": "urltest", "outbounds": ["jp", "us"]})
        cfg["outbounds"][2] = {"tag": "pick", "type": "selector",
                               "outbounds": ["auto", "jp"], "default": "auto"}
        obs = build_outbounds(cfg)
        sel = obs["pick"]
        self.assertEqual(sel.selected, "auto")
        # resolve 穿透到 urltest 选的叶子（jp 或 us）
        self.assertIn(sel.resolve_leaf().tag, ("jp", "us"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
