"""
Clash 兼容 API 端点单元测试。

直接调 endpoint handler（async）传 mock Request + APIContext，断言 Response，
不起真 TCP server——快、稳、无端口冲突。

覆盖会话内改的端点：
  - /proxies：合成 GLOBAL Selector（排除 block）+ DIRECT/REJECT alias（B5）
  - /proxies/{name}/delay：读 latency_ms（B5）
  - /proxies/{name} PUT：no-op 204（B5）
  - /connections：memory 字段填 RSS（B5）
  - /configs：log-level 读顶层 log_levels.default（自审纰漏）
  - /rules：FINAL 行 + router.default
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api.http_proto import Request, Response  # noqa: E402
from core.api.server import APIContext, APIServer  # noqa: E402
from core.api import clash_endpoints as ce  # noqa: E402


def _req(method="GET", path="/", query=None, headers=None, body=b""):
    return Request(method=method, path=path, query=query or {},
                   headers=headers or {}, body=body)


def _run(coro):
    return asyncio.run(coro)


def _body(resp: Response) -> dict:
    return json.loads(resp.body.decode())


class _MockOutbound:
    def __init__(self, tag, otype, latency=None, healthy=True):
        self.tag = tag
        self.type = otype
        self.latency_ms = latency
        self.is_healthy = healthy
        self.latency_age_sec = 0.0
        if otype == "mirage":
            class _Pool:
                _cfg = {"server_host": "1.2.3.4", "server_port": 443}
            self._pool = _Pool()


def _ctx(outbounds=None, cfg=None, registry=None, router_engine=None):
    return APIContext(version="0.4.43", cfg=cfg or {},
                      outbounds=outbounds, registry=registry,
                      router_engine=router_engine)


def _std_outbounds():
    return {
        "proxy": _MockOutbound("proxy", "mirage", latency=42),
        "direct": _MockOutbound("direct", "direct"),
        "block": _MockOutbound("block", "block"),
    }


class TestProxies(unittest.TestCase):
    def test_global_selector_synthesized(self):
        resp = _run(ce._proxies(_req(), _ctx(_std_outbounds())))
        proxies = _body(resp)["proxies"]
        self.assertIn("GLOBAL", proxies)
        self.assertEqual(proxies["GLOBAL"]["type"], "Selector")

    def test_global_excludes_block(self):
        # 误选 REJECT 迷惑性强（PUT no-op），GLOBAL.all 必须排除 block
        resp = _run(ce._proxies(_req(), _ctx(_std_outbounds())))
        g = _body(resp)["proxies"]["GLOBAL"]
        self.assertNotIn("block", g["all"])
        self.assertIn("proxy", g["all"])
        self.assertIn("direct", g["all"])

    def test_global_now_prefers_non_direct(self):
        resp = _run(ce._proxies(_req(), _ctx(_std_outbounds())))
        self.assertEqual(_body(resp)["proxies"]["GLOBAL"]["now"], "proxy")

    def test_direct_reject_aliases(self):
        resp = _run(ce._proxies(_req(), _ctx(_std_outbounds())))
        proxies = _body(resp)["proxies"]
        self.assertEqual(proxies["DIRECT"]["name"], "DIRECT")
        self.assertEqual(proxies["REJECT"]["name"], "REJECT")

    def test_empty_outbounds_no_crash(self):
        resp = _run(ce._proxies(_req(), _ctx({})))
        self.assertEqual(_body(resp)["proxies"], {})


class TestProxyDelay(unittest.TestCase):
    def test_delay_reads_latency(self):
        resp = _run(ce._proxy_delay(_req(), _ctx(_std_outbounds()), name="proxy"))
        b = _body(resp)
        self.assertEqual(b["delay"], 42)
        self.assertEqual(b["meanDelay"], 42)

    def test_delay_no_latency_is_400(self):
        # block 无 latency → 400（yacd 期望的错误形态）
        resp = _run(ce._proxy_delay(_req(), _ctx(_std_outbounds()), name="block"))
        self.assertEqual(resp.status, 400)

    def test_delay_unknown_proxy_404(self):
        resp = _run(ce._proxy_delay(_req(), _ctx(_std_outbounds()), name="nope"))
        self.assertEqual(resp.status, 404)


class TestProxySelect(unittest.TestCase):
    def test_put_known_proxy_204(self):
        resp = _run(ce._proxy_select(_req(method="PUT"), _ctx(_std_outbounds()), name="proxy"))
        self.assertEqual(resp.status, 204)

    def test_put_global_204(self):
        resp = _run(ce._proxy_select(_req(method="PUT"), _ctx(_std_outbounds()), name="GLOBAL"))
        self.assertEqual(resp.status, 204)

    def test_put_unknown_404(self):
        resp = _run(ce._proxy_select(_req(method="PUT"), _ctx(_std_outbounds()), name="nope"))
        self.assertEqual(resp.status, 404)


class _MockSelector:
    type = "selector"
    def __init__(self, tag, children, selected):
        self.tag = tag
        self._children = [_MockOutbound(c, "mirage", latency=10) for c in children]
        self._selected = selected
        self.is_healthy = True
        self.latency_ms = 10
        self.latency_age_sec = 0.0
    @property
    def selected(self):
        return self._selected
    def select(self, tag):
        if any(c.tag == tag for c in self._children):
            self._selected = tag
            return True
        return False


class TestProxySelectorSwitch(unittest.TestCase):
    def _ctx_with_selector(self):
        obs = {
            "jp": _MockOutbound("jp", "mirage", latency=20),
            "us": _MockOutbound("us", "mirage", latency=30),
            "pick": _MockSelector("pick", ["jp", "us"], selected="jp"),
        }
        return _ctx(obs)

    def test_put_selector_switches(self):
        ctx = self._ctx_with_selector()
        req = _req(method="PUT", body=b'{"name": "us"}')
        resp = _run(ce._proxy_select(req, ctx, name="pick"))
        self.assertEqual(resp.status, 204)
        self.assertEqual(ctx.outbounds["pick"].selected, "us")

    def test_put_selector_invalid_child_400(self):
        ctx = self._ctx_with_selector()
        req = _req(method="PUT", body=b'{"name": "ghost"}')
        resp = _run(ce._proxy_select(req, ctx, name="pick"))
        self.assertEqual(resp.status, 400)
        self.assertEqual(ctx.outbounds["pick"].selected, "jp")   # 不变

    def test_put_selector_missing_name_400(self):
        ctx = self._ctx_with_selector()
        req = _req(method="PUT", body=b'{}')
        resp = _run(ce._proxy_select(req, ctx, name="pick"))
        self.assertEqual(resp.status, 400)

    def test_put_non_selector_noop_204(self):
        # 普通 mirage 节点（非 selector）→ no-op 204
        ctx = self._ctx_with_selector()
        req = _req(method="PUT", body=b'{"name": "x"}')
        resp = _run(ce._proxy_select(req, ctx, name="jp"))
        self.assertEqual(resp.status, 204)

    def test_selector_reported_in_proxies(self):
        ctx = self._ctx_with_selector()
        resp = _run(ce._proxies(_req(), ctx))
        pick = _body(resp)["proxies"]["pick"]
        self.assertEqual(pick["type"], "Selector")
        self.assertEqual(pick["now"], "jp")
        self.assertEqual(set(pick["all"]), {"jp", "us"})


class TestConnections(unittest.TestCase):
    def test_memory_field_present(self):
        # registry=None 分支也应带 memory（RSS）
        resp = _run(ce._connections(_req(), _ctx(_std_outbounds())))
        b = _body(resp)
        self.assertIn("memory", b)
        self.assertIn("downloadTotal", b)
        self.assertIn("uploadTotal", b)
        self.assertEqual(b["connections"], [])

    def test_memory_is_int_and_nonneg(self):
        resp = _run(ce._connections(_req(), _ctx(_std_outbounds())))
        self.assertIsInstance(_body(resp)["memory"], int)
        self.assertGreaterEqual(_body(resp)["memory"], 0)


class TestConfigsLogLevel(unittest.TestCase):
    def test_log_level_from_top_log_levels(self):
        cfg = {"log_levels": {"default": "DEBUG"}, "socks5_port": 1080}
        resp = _run(ce._configs(_req(), _ctx({}, cfg=cfg)))
        self.assertEqual(_body(resp)["log-level"], "debug")

    def test_log_level_fallback_info(self):
        resp = _run(ce._configs(_req(), _ctx({}, cfg={"socks5_port": 1080})))
        self.assertEqual(_body(resp)["log-level"], "info")

    def test_configs_redacts_password(self):
        cfg = {"password": "supersecret", "socks5_port": 1080,
               "outbounds": [{"tag": "proxy", "type": "mirage", "password": "leak"}]}
        resp = _run(ce._configs(_req(), _ctx({}, cfg=cfg)))
        raw = resp.body.decode()
        self.assertNotIn("supersecret", raw)
        self.assertNotIn("leak", raw)


class _MockRouter:
    """最小 router_engine：rules + default。"""
    def __init__(self, rules, default):
        self.rules = rules
        self.default = default


class TestRules(unittest.TestCase):
    def test_rules_append_final_match(self):
        router = _MockRouter(
            [{"type": "DomainSuffix", "payload": ".x.com", "proxy": "proxy", "invert": False}],
            default="direct")
        resp = _run(ce._rules(_req(), _ctx({}, router_engine=router)))
        rules = _body(resp)["rules"]
        self.assertEqual(rules[-1]["type"], "Match")
        self.assertEqual(rules[-1]["proxy"], "direct")   # router.default

    def test_rules_no_router(self):
        resp = _run(ce._rules(_req(), _ctx({})))
        self.assertEqual(_body(resp)["rules"], [])


class TestCors(unittest.TestCase):
    """B8：Allow-Origin 必须回显具体 Origin（非 *），配 Vary，才与 Credentials 合规。"""

    def _server(self, cors=None):
        cfg = {"api": {"listen": "127.0.0.1:0", "secret": "s",
                       "cors": cors if cors is not None else ["*"]}}
        return APIServer(cfg, _ctx({}, cfg=cfg))

    def _headers(self, srv, origin):
        req = _req(headers={"origin": origin})
        resp = Response(status=200)
        srv._inject_cors(req, resp)
        return dict(resp.extra_headers)

    def test_wildcard_echoes_specific_origin(self):
        h = self._headers(self._server(["*"]), "http://yacd.example")
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "http://yacd.example")
        self.assertNotEqual(h.get("Access-Control-Allow-Origin"), "*")

    def test_vary_origin_present(self):
        h = self._headers(self._server(["*"]), "http://yacd.example")
        self.assertEqual(h.get("Vary"), "Origin")

    def test_credentials_true(self):
        h = self._headers(self._server(["*"]), "http://yacd.example")
        self.assertEqual(h.get("Access-Control-Allow-Credentials"), "true")

    def test_no_origin_header_no_cors(self):
        srv = self._server(["*"])
        req = _req()   # 无 Origin
        resp = Response(status=200)
        srv._inject_cors(req, resp)
        self.assertEqual(resp.extra_headers, [])

    def test_origin_not_in_whitelist_blocked(self):
        # 显式白名单（非 *），不匹配的 Origin 不注入 CORS
        h = self._headers(self._server(["http://allowed.example"]), "http://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_whitelisted_origin_allowed(self):
        h = self._headers(self._server(["http://allowed.example"]), "http://allowed.example")
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "http://allowed.example")


class TestVersion(unittest.TestCase):
    def test_version_reports_meta(self):
        resp = _run(ce._version(_req(), _ctx({}, cfg={})))
        b = _body(resp)
        self.assertEqual(b["version"], "0.4.43")
        self.assertTrue(b["meta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
