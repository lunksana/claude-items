"""
install.sh 生成的配置自动校验。

install.sh 是 1000+ 行 bash 生成 JSON，过去只人工抽查，踩过多次字段名回归
（geosite vs rule_set、dns 顶层 vs dns 块、route.default 等）。本测试把
install.sh 的 config 生成函数 source 出来，用各种参数组合调用，再用真正的
load_config 验证产物——任何字段名 / schema 回归当场暴露。

机制：
  1. strip install.sh 末行的 `main "$@"`，source 剩余（只定义函数，不跑交互）
  2. 设好 EFFECTIVE_* / LOG_* / DNS_* / ROUTE_ADV_* 环境
  3. 调 write_client_config / write_server_config 写出 JSON
  4. load_config(JSON) 断言：不抛 ConfigError、无 "unknown top-level" 告警

需要 bash。非 Linux / 无 bash 环境自动 skip。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "install.sh")

from core.config import load_config, ConfigError  # noqa: E402


def _bash_available() -> bool:
    return shutil.which("bash") is not None and os.path.exists(INSTALL_SH)


# 公共 bash 前奏：source install.sh 的函数 + 设好生成所需环境。
# {extra} 处由各用例插入额外变量（如 ROUTE_ADV_RULES）。
_DRIVER = r"""
set -e
cd "{root}"
head -n -1 install.sh > "{work}/i.sh"
source "{work}/i.sh"
EFFECTIVE_ETC="{work}"
EFFECTIVE_LOG_DIR="{work}/log"
EFFECTIVE_GEOSITE_DIR="/var/lib/mirage/geosite"
mkdir -p "$EFFECTIVE_LOG_DIR"
LOG_FORMAT=text
LOG_LEVEL=INFO
LOGROTATE_ENABLED=no
INSTALL_MODE=system
DNS_LISTEN_HOST=127.0.0.1
DNS_LISTEN_PORT=5353
DNS_CN=119.29.29.29
DNS_REMOTE="1.1.1.1:53"
{extra}
{call}
"""


class _GenMixin:
    def setUp(self):
        if not _bash_available():
            self.skipTest("bash / install.sh 不可用")
        self.work = tempfile.mkdtemp(prefix="mirage_inst_")

    def tearDown(self):
        shutil.rmtree(getattr(self, "work", ""), ignore_errors=True)

    def _run_driver(self, call: str, extra: str = ""):
        script = _DRIVER.format(root=ROOT, work=self.work, call=call, extra=extra)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        if r.returncode != 0:
            self.fail(f"install.sh 生成失败:\n{r.stderr[-1500:]}")

    def _validate(self, filename: str, expect_schema_v1: bool):
        path = os.path.join(self.work, filename)
        self.assertTrue(os.path.exists(path), f"{filename} 未生成")
        # 1) 合法 JSON
        with open(path) as f:
            cfg = json.load(f)
        # 2) load_config 不抛 + 捕获告警
        warns: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                if rec.levelno >= logging.WARNING:
                    warns.append(rec.getMessage())

        root = logging.getLogger()
        h = _Cap()
        root.addHandler(h)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            load_config(path)
        except ConfigError as e:
            self.fail(f"{filename} load_config 失败: {e}")
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        unknown = [w for w in warns if "unknown top-level" in w]
        self.assertEqual(unknown, [], f"{filename} 有 unknown 告警: {unknown}")
        if expect_schema_v1:
            self.assertEqual(cfg.get("schema_version"), 1)
        return cfg


class TestClientConfigGen(_GenMixin, unittest.TestCase):
    # write_client_config <server_host> <server_port> <password> <camouflage>
    #   <listen_host> <socks5_port> <routing_preset> <dns_preset>
    #   <tproxy_port> <enable_api> <api_secret>

    def _gen_client(self, routing, dns, tproxy="0", api="no", secret="", extra=""):
        call = (f'write_client_config "1.2.3.4" 443 "pw" "www.bing.com" '
                f'"0.0.0.0" 7890 "{routing}" "{dns}" {tproxy} "{api}" "{secret}"')
        self._run_driver(call, extra=extra)
        return self._validate("config_client.json", expect_schema_v1=True)

    def test_china_split_dns_split(self):
        cfg = self._gen_client("china_split", "china_split")
        self.assertEqual(cfg["route"]["default"], "proxy")
        # rule_set（不是 geosite）+ geo 源声明都在
        self.assertIn("geosite_sources", cfg)
        fields = {k for r in cfg["route"]["rules"] for k in r if k != "outbound"}
        self.assertIn("rule_set", fields)
        self.assertNotIn("geosite", fields)   # B4：必须是 rule_set

    def test_transparent_full_proxy(self):
        cfg = self._gen_client("transparent", "full_proxy")
        self.assertEqual(cfg["route"]["rules"], [])

    def test_custom_dns_off(self):
        cfg = self._gen_client("custom", "off")
        # dns off → 不应有 dns 块
        self.assertNotIn("dns", cfg)

    def test_with_tproxy_and_api(self):
        cfg = self._gen_client("china_split", "china_split",
                               tproxy="1081", api="yes", secret="sek")
        self.assertEqual(cfg.get("tproxy_port"), 1081)
        self.assertEqual(cfg["api"]["listen"], "127.0.0.1:9090")

    def test_dns_block_is_structured(self):
        # B3：DNS 字段必须在 dns 块里（不是顶层 cn_dns/dns_listen_*）
        cfg = self._gen_client("china_split", "china_split")
        self.assertIn("dns", cfg)
        self.assertEqual(cfg["dns"]["listen"], "127.0.0.1:5353")
        self.assertNotIn("cn_dns", cfg)         # 不再污染顶层
        self.assertNotIn("dns_listen_host", cfg)

    def test_advanced_routing(self):
        # advanced 模式从 ROUTE_ADV_RULES 数组拼 JSON
        extra = (
            'ROUTE_ADV_RULES=('
            '\'{"ip_cidr": ["127.0.0.0/8"], "outbound": "direct"}\' '
            '\'{"rule_set": ["loyalsoldier:category-ads-all"], "outbound": "block"}\' '
            '\'{"rule_set": ["loyalsoldier:cn"], "outbound": "direct"}\' '
            '\'{"geoip": ["loyalsoldier:cn"], "outbound": "direct"}\')\n'
            'ROUTE_ADV_DEFAULT=proxy'
        )
        cfg = self._gen_client("advanced", "china_split", extra=extra)
        self.assertEqual(cfg["route"]["default"], "proxy")
        self.assertEqual(len(cfg["route"]["rules"]), 4)
        # route.default 指向真实 outbound
        tags = {o["tag"] for o in cfg["outbounds"]}
        self.assertIn(cfg["route"]["default"], tags)

    def test_all_routing_dns_combos_load_clean(self):
        # 矩阵：每个组合都得干净加载（无 ConfigError / 无 unknown 告警）
        for routing in ("china_split", "transparent", "custom"):
            for dns in ("off", "china_split", "full_proxy"):
                with self.subTest(routing=routing, dns=dns):
                    self._gen_client(routing, dns)


class TestServerConfigGen(_GenMixin, unittest.TestCase):
    # write_server_config <listen_host> <listen_port> <password> <camouflage> <brutal_rate_bps>

    def test_server_config_loads_clean(self):
        call = 'write_server_config "0.0.0.0" 443 "pw" "www.apple.com" 0'
        self._run_driver(call)
        # 服务端仍用 legacy 平铺 schema（无 schema_version）
        cfg = self._validate("config_server.json", expect_schema_v1=False)
        self.assertEqual(cfg["listen_port"], 443)
        self.assertEqual(cfg["camouflage_host"], "www.apple.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
