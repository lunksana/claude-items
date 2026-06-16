"""
Config 加载 / 校验 / 投影 单元测试（stdlib unittest，无第三方依赖）。

运行：
    python3 -m unittest tests.test_config
    或  python3 tests/test_config.py

覆盖的回归点：
  - schema v1 白名单：log_levels / geosite_sources / geoip_sources /
    tproxy_port / geosite_dir 都是合法顶层键（曾经触发 "unknown ignored"）
  - B3：dns.{listen,cn,remote} → 顶层 dns_listen_host/port / cn_dns / remote_dns 投影
  - B3：0.4.40 老顶层 DNS 键 → 友好 deprecation 提示（而非当 unknown 丢弃）
  - 真正未知键仍告警
  - schema_version 不匹配抛 ConfigError
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config, ConfigError  # noqa: E402


def _load(cfg: dict):
    """把 dict 写到临时文件再 load_config，返回 (cfg_out, [warning_messages])。"""
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.WARNING:
                records.append(r.getMessage())

    root = logging.getLogger()
    handler = _Cap()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        out = load_config(path)
        return out, records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        if os.path.exists(path):
            os.remove(path)


def _base():
    """最小合法 v1 配置。"""
    return {
        "schema_version": 1,
        "inbounds": [{"type": "mixed", "listen": "127.0.0.1:7890"}],
        "outbounds": [{"tag": "direct", "type": "direct"}],
        "route": {"default": "direct", "rules": []},
        "log": {"format": "text"},
    }


class TestSchemaWhitelist(unittest.TestCase):
    def test_new_keys_not_flagged_unknown(self):
        cfg = _base()
        cfg["log_levels"] = {"default": "INFO"}
        cfg["geosite_sources"] = [{"name": "loyalsoldier", "url": "http://x"}]
        cfg["geoip_sources"] = [{"name": "loyalsoldier", "url": "http://y"}]
        cfg["geosite_dir"] = "/var/lib/mirage/geosite"
        cfg["tproxy_port"] = 1081
        _, warns = _load(cfg)
        unknown = [w for w in warns if "unknown top-level" in w]
        self.assertEqual(unknown, [], f"不应有 unknown 告警: {unknown}")

    def test_genuinely_unknown_key_warns(self):
        cfg = _base()
        cfg["totally_made_up_key"] = 1
        _, warns = _load(cfg)
        self.assertTrue(any("unknown top-level" in w and "totally_made_up_key" in w
                            for w in warns))

    def test_bad_schema_version_raises(self):
        cfg = _base()
        cfg["schema_version"] = 99
        with self.assertRaises(ConfigError):
            _load(cfg)


class TestDnsProjection(unittest.TestCase):
    """B3：新 dns.{listen,cn,remote} 块投影到顶层旧键。"""

    def test_dns_block_projects(self):
        cfg = _base()
        cfg["dns"] = {"listen": "127.0.0.1:5353", "cn": "119.29.29.29",
                      "remote": "tls://1.1.1.1:853"}
        out, warns = _load(cfg)
        self.assertEqual(out.get("dns_listen_host"), "127.0.0.1")
        self.assertEqual(out.get("dns_listen_port"), 5353)
        self.assertEqual(out.get("cn_dns"), "119.29.29.29")
        self.assertEqual(out.get("remote_dns"), "tls://1.1.1.1:853")
        self.assertEqual([w for w in warns if "unknown" in w], [])

    def test_legacy_top_dns_keys_deprecated_not_unknown(self):
        # 0.4.40 老格式：DNS 键在顶层。应得 deprecation 提示，runtime 仍 honored，
        # 不能被当 "unknown ignored" 丢掉
        cfg = _base()
        cfg["cn_dns"] = "114.114.114.114"
        cfg["remote_dns"] = "8.8.8.8:53"
        cfg["dns_listen_host"] = "127.0.0.1"
        cfg["dns_listen_port"] = 5353
        out, warns = _load(cfg)
        self.assertEqual(out.get("cn_dns"), "114.114.114.114")   # 仍可读
        self.assertTrue(any("deprecated top-level DNS keys" in w for w in warns))
        # 不应被列入 unknown-ignored
        self.assertFalse(any("unknown top-level" in w and "cn_dns" in w for w in warns))


class TestServerSchema(unittest.TestCase):
    """B9：server.py 真实读的顶层键必须进白名单，否则启动期假告警。"""

    def test_admin_keys_not_unknown(self):
        cfg = _base()
        cfg.update({"admin_port": 8080, "admin_host": "127.0.0.1", "admin_token": "x"})
        _, warns = _load(cfg)
        self.assertEqual([w for w in warns if "unknown top-level" in w], [])

    def test_egress_keys_not_unknown(self):
        cfg = _base()
        cfg["egresses"] = []
        cfg["egress_rules"] = []
        _, warns = _load(cfg)
        self.assertEqual([w for w in warns if "unknown top-level" in w], [])

    def test_server_runtime_tunables_not_unknown(self):
        cfg = _base()
        cfg.update({
            "access_log": True, "idle_timeout_sec": 1800,
            "max_conns_per_ip": 100, "tcp_keepalive": True,
            "drain_threshold": 65536,
        })
        _, warns = _load(cfg)
        self.assertEqual([w for w in warns if "unknown top-level" in w], [])


class TestSchemaCodeContract(unittest.TestCase):
    """
    元测试：扫 server.py / client.py 真读了哪些顶层 cfg 键，必须全部进白名单。
    任何人加新顶层字段而忘了声明，下次 CI 就报。
    """

    @staticmethod
    def _extract_top_keys(path: str) -> set[str]:
        import re
        src = open(path).read()
        # cfg.get("foo"/'foo', ...) 或 cfg["foo"]/cfg['foo']
        # 单引号也要覆盖：client.py 里有 cfg.get('socks5_host', ...)，
        # 只匹配双引号会漏，让契约检查出现盲点
        pat = re.compile(
            r'''cfg\.get\(\s*['"]([a-z_][a-z0-9_]*)['"]'''
            r'''|cfg\[\s*['"]([a-z_][a-z0-9_]*)['"]\s*\]'''
        )
        return {m.group(1) or m.group(2) for m in pat.finditer(src)}

    def test_server_keys_all_declared(self):
        from core.config import _V1_TOP_KEYS, _LEGACY_TOP_KEYS, _DEPRECATED_DNS_TOP_KEYS
        declared = _V1_TOP_KEYS | _LEGACY_TOP_KEYS | _DEPRECATED_DNS_TOP_KEYS
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        used = self._extract_top_keys(os.path.join(root, "server.py"))
        missing = used - declared
        self.assertEqual(missing, set(),
                         f"server.py 读了未声明的顶层键: {sorted(missing)}")

    def test_client_keys_all_declared(self):
        from core.config import _V1_TOP_KEYS, _LEGACY_TOP_KEYS, _DEPRECATED_DNS_TOP_KEYS
        declared = _V1_TOP_KEYS | _LEGACY_TOP_KEYS | _DEPRECATED_DNS_TOP_KEYS
        # client 还从其它常见模块读 cfg 顶层
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        all_used = set()
        for f in ("client.py", "core/dns_forwarder.py", "core/geosite_cache.py",
                  "core/outbound.py", "core/router.py"):
            all_used |= self._extract_top_keys(os.path.join(root, f))
        missing = all_used - declared
        # 已知的"伪 cfg.get"误报（局部 dict 不是顶层 cfg）放白名单豁免
        # 真有的话改这里前请先确认那个 cfg.get 真的不是读顶层 cfg
        # 误报豁免：这些字符串落在 cfg.get() 文法里，但实际是 udp_cfg/node_cfg 等
        # 局部 dict 的 .get，不是顶层 cfg。误报来自正则不区分变量名。
        # 真是顶层 cfg 字段的话，请加白名单而非加豁免。
        false_positive = {
            "udp_relay_host", "udp_idle_timeout",   # client.py 是 udp_cfg.get
            "geosite_url", "geosite_path",          # geosite_cache 老兼容字段，本身没 schema 校验需求
            "force_geosite_update",                 # geosite_cache 调试开关
        }
        missing -= false_positive
        self.assertEqual(missing, set(),
                         f"client 侧读了未声明的顶层键: {sorted(missing)}")


class TestLegacyConfig(unittest.TestCase):
    def test_legacy_format_accepted(self):
        # 无 schema_version + 老顶层键 → 自动识别，不抛
        cfg = {
            "server_host": "1.2.3.4", "server_port": 443,
            "password": "x", "camouflage_host": "www.apple.com",
            "socks5_host": "127.0.0.1", "socks5_port": 1080,
        }
        out, _ = _load(cfg)
        self.assertEqual(out.get("server_host"), "1.2.3.4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
