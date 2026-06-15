"""
Router 单元测试（stdlib unittest，无第三方依赖）。

运行：
    python3 -m unittest tests.test_router   （在项目根目录）
    或  python3 tests/test_router.py

覆盖的回归点（每条对应一个曾经踩过的 bug）：
  - B6：route.default 必须被读取（曾经只读 route.final，default 是死键）
  - B4：rule_set → GEOSITE 字段映射（曾经写 "geosite" 被当未知字段跳过）
  - B7：空 geo 时非 geo 规则（domain / ip_cidr）仍正常构建并生效
  - 默认出口优先级：default > final > legacy_action_map(PROXY)
"""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 静音 router 构建期的 INFO/WARNING（geo not available 等是被测的预期行为）。
# 只调 "router" logger 的级别，不用 logging.disable()——后者是进程级全局开关，
# 会连带压掉其它测试模块（如 test_config）需要捕获的告警，造成合跑时串扰。
logging.getLogger("router").setLevel(logging.ERROR)

from core.router import build_router, PROXY  # noqa: E402

# 典型 client 的 legacy 映射：PROXY→proxy / DIRECT→direct / REJECT→block
_LM = {"PROXY": "proxy", "DIRECT": "direct", "REJECT": "block"}
_VALID = {"proxy", "direct", "block", "auto"}


def _build(route_block, legacy=_LM, valid=_VALID):
    return build_router({"route": route_block}, {}, {},
                        valid_actions=valid, legacy_action_map=legacy)


class TestDefaultPrecedence(unittest.TestCase):
    """B6：兜底出口键解析 default > final > legacy(PROXY)。"""

    def test_default_key_honored(self):
        # 曾经的 bug：route.default 被忽略，恒回退到 'proxy'
        r = _build({"default": "direct", "rules": []})
        self.assertEqual(r.default, "direct")

    def test_default_auto_no_proxy_tag(self):
        # 示例配置场景：default=auto，没有 proxy tag。修复前会回退到不存在的
        # 'proxy' → 全部未命中流量被拒
        r = _build({"default": "auto", "rules": []})
        self.assertEqual(r.default, "auto")

    def test_final_alias_fallback(self):
        # sing-box 风格 final 别名仍支持
        r = _build({"final": "direct", "rules": []})
        self.assertEqual(r.default, "direct")

    def test_default_wins_over_final(self):
        r = _build({"default": "direct", "final": "auto", "rules": []})
        self.assertEqual(r.default, "direct")

    def test_neither_falls_back_to_legacy_proxy(self):
        r = _build({"rules": []})
        self.assertEqual(r.default, "proxy")   # legacy_action_map[PROXY]

    def test_no_legacy_map_falls_back_to_direct(self):
        # legacy_action_map 为空 → initial_default 用 "direct"
        r = build_router({"route": {"rules": []}}, {}, {},
                         valid_actions=_VALID, legacy_action_map={})
        self.assertEqual(r.default, "direct")


class TestStructuredRules(unittest.TestCase):
    """结构化 rules 解析 + 字段映射。"""

    def test_domain_suffix_matches(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".openai.com"], "outbound": "proxy"}]})
        action, _ = r.match("chat.openai.com")
        self.assertEqual(action, "proxy")

    def test_ip_cidr_matches(self):
        r = _build({"default": "proxy", "rules": [
            {"ip_cidr": ["10.0.0.0/8"], "outbound": "direct"}]})
        action, _ = r.match("10.1.2.3")
        self.assertEqual(action, "direct")

    def test_unmatched_uses_default(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".openai.com"], "outbound": "proxy"}]})
        action, src = r.match("example.com")
        self.assertEqual(action, "direct")
        self.assertEqual(src, "FINAL")

    def test_array_is_or_semantics(self):
        # 一个字段里多个值 = OR，展开成多条
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com", ".b.com"], "outbound": "proxy"}]})
        self.assertEqual(r.match("x.a.com")[0], "proxy")
        self.assertEqual(r.match("y.b.com")[0], "proxy")

    def test_invalid_outbound_skipped(self):
        # outbound 不在 valid_actions → 该规则被跳过，不影响其它
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".x.com"], "outbound": "nonexistent"},
            {"domain_suffix": [".y.com"], "outbound": "proxy"}]})
        self.assertEqual(r.match("a.x.com")[0], "direct")   # skipped → default
        self.assertEqual(r.match("a.y.com")[0], "proxy")


class TestEmptyGeoSurvival(unittest.TestCase):
    """B7：空 geo（首启 / 下载失败）时，rule_set/geoip 跳过但非 geo 规则存活。"""

    def test_non_geo_rules_survive_empty_geo(self):
        r = _build({"default": "proxy", "rules": [
            {"domain_suffix": [".openai.com"], "outbound": "proxy"},
            {"ip_cidr": ["10.0.0.0/8"], "outbound": "direct"},
            {"rule_set": ["loyalsoldier:cn"], "outbound": "direct"},   # 跳过
            {"geoip": ["loyalsoldier:cn"], "outbound": "direct"},      # 跳过
        ]})
        # 应保留 2 条非 geo 规则
        self.assertEqual(len(r.rules), 2)
        # 非 geo 规则即时生效
        self.assertEqual(r.match("chat.openai.com")[0], "proxy")
        self.assertEqual(r.match("10.0.0.1")[0], "direct")

    def test_rule_set_field_is_geosite(self):
        # B4：rule_set 必须被识别成 criterion 字段（而非 "no criterion field"）
        # 空 geo 下它会被跳过，但不能因为"无 criterion"被跳——通过规则计数间接验证：
        # 一条 rule_set + 一条 domain，空 geo 下应剩 1 条（domain），不是 0
        r = _build({"default": "proxy", "rules": [
            {"rule_set": ["loyalsoldier:gfw"], "outbound": "proxy"},
            {"domain": ["example.com"], "outbound": "direct"},
        ]})
        self.assertEqual(len(r.rules), 1)
        self.assertEqual(r.match("example.com")[0], "direct")


class TestMultiCriterionDefaultOr(unittest.TestCase):
    """多 criterion 默认 OR：任一字段命中即命中（等价于拆成多条独立规则）。"""

    def test_multi_field_is_or_by_default(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".google.com"], "domain_keyword": ["video"],
             "outbound": "proxy"}]})
        # 任一满足即命中
        self.assertEqual(r.match("mail.google.com")[0], "proxy")    # 只 suffix
        self.assertEqual(r.match("video.youtube.com")[0], "proxy")  # 只 keyword
        self.assertEqual(r.match("video.google.com")[0], "proxy")   # 两者
        self.assertEqual(r.match("example.com")[0], "direct")       # 都不满足

    def test_multi_field_or_expands_to_independent_rules(self):
        # 默认 OR → 两个字段各展开成独立规则
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com"], "domain_keyword": ["x"], "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 2)

    def test_multi_field_or_survives_empty_geo(self):
        # 默认 OR 下，空 geo 的 geoip 字段被跳过，但 domain 字段仍生效（不像 AND 整条丢）
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".google.com"], "geoip": ["loyalsoldier:us"],
             "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 1)   # 只剩 domain_suffix 那条
        self.assertEqual(r.match("x.google.com")[0], "proxy")


class TestAndRules(unittest.TestCase):
    """显式 "mode": "and" → AND 复合：字段内 OR、字段间 AND。"""

    def test_and_both_must_match(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".google.com"], "domain_keyword": ["video"],
             "mode": "and", "outbound": "proxy"}]})
        self.assertEqual(r.match("video.google.com")[0], "proxy")   # 两者都满足
        self.assertEqual(r.match("mail.google.com")[0], "direct")   # 缺 keyword
        self.assertEqual(r.match("video.youtube.com")[0], "direct")  # 缺 suffix

    def test_and_field_internal_or(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com", ".b.com"], "domain_keyword": ["x"],
             "mode": "and", "outbound": "proxy"}]})
        self.assertEqual(r.match("x.a.com")[0], "proxy")
        self.assertEqual(r.match("x.b.com")[0], "proxy")
        self.assertEqual(r.match("y.a.com")[0], "direct")   # suffix 命中但缺 keyword

    def test_and_produces_single_rule(self):
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com"], "domain_keyword": ["x"],
             "mode": "and", "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 1)   # 一条复合规则，不是两条

    def test_and_skipped_if_a_field_empty_under_empty_geo(self):
        # rule_set/geoip 在空 geo 下展开为空 → 整条 AND 跳过（不会半命中）
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".google.com"], "geoip": ["loyalsoldier:us"],
             "mode": "and", "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 0)
        self.assertEqual(r.match("x.google.com")[0], "direct")

    def test_cidr_and_domain(self):
        # IP-literal host：CIDR + 不可能的 keyword → 不命中
        r = _build({"default": "direct", "rules": [
            {"ip_cidr": ["10.0.0.0/8"], "domain_keyword": ["zzz"],
             "mode": "and", "outbound": "proxy"}]})
        # 10.x 命中 CIDR 但 host="10.0.0.1" 不含 zzz → AND 失败
        self.assertEqual(r.match("10.0.0.1")[0], "direct")

    def test_mode_or_explicit_is_or(self):
        # 显式 "mode": "or" 等同默认
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com"], "domain_keyword": ["x"],
             "mode": "or", "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 2)
        self.assertEqual(r.match("y.a.com")[0], "proxy")   # 只 suffix 也命中

    def test_invalid_mode_falls_back_or(self):
        # 非法 mode → 告警 + 按 or 处理（不报错退出）
        r = _build({"default": "direct", "rules": [
            {"domain_suffix": [".a.com"], "domain_keyword": ["x"],
             "mode": "xor", "outbound": "proxy"}]})
        self.assertEqual(len(r.rules), 2)   # 退回 OR


class TestClashRulesView(unittest.TestCase):
    """Router.rules 给 Clash API 的只读视图。"""

    def test_rules_have_clash_shape(self):
        r = _build({"default": "proxy", "rules": [
            {"domain_suffix": [".openai.com"], "outbound": "proxy"}]})
        rules = r.rules
        self.assertEqual(len(rules), 1)
        self.assertIn("type", rules[0])
        self.assertIn("payload", rules[0])
        self.assertIn("proxy", rules[0])
        self.assertEqual(rules[0]["proxy"], "proxy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
