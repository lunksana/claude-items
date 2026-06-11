"""
Clash 兼容端点。本 phase（P1）只实现 /version 和 /configs。

Clash UI 的最低存活要求：
  GET /version  → 让 UI 显示连接成功 + 解锁 meta 字段
  GET /configs  → 让 UI 在启动时拿到 port/socks-port/mode 等
"""

from __future__ import annotations

from typing import Any

from .http_proto import Request, Response
from .router import Router
from .server import APIContext, json_response

# /configs 响应里**绝不**返回的字段（脱敏）
_SECRET_FIELDS_TOP = {"password", "secret"}                # 兼容 legacy 顶层
_SECRET_FIELDS_API = {"secret"}                            # api.secret
_SECRET_FIELDS_OUTBOUND = {"password", "credential"}       # outbound 字段


def register(router: Router, ctx: APIContext) -> None:
    router.add("GET", "/version", _version)
    router.add("GET", "/configs", _configs)
    router.add("PUT", "/configs", _configs_reload)
    router.add("GET", "/connections", _connections)
    router.add("GET", "/proxies", _proxies)
    router.add("GET", "/proxies/{name}", _proxy_one)
    router.add("GET", "/rules", _rules)


async def _version(req: Request, ctx: APIContext) -> Response:
    """
    Clash UI 拿到 meta:true 后会解锁所有 meta 分支特性（dns/tun/sniff/...），
    pyrealiy 已经原生支持其中大部分形态，索性按 meta 报。
    """
    return json_response({
        "version": ctx.version,
        "meta": True,
        "premium": False,
    })


async def _configs(req: Request, ctx: APIContext) -> Response:
    """
    返回 Clash UI 期望的字段（即便 pyrealiy 不全用，Yacd / metacubexd 都看这些
    字段决定是否启用某些 UI 控件）。

    内容包含 pyrealiy 实际生效的网络口岸 + 简化后的 cfg 摘要（脱敏后）。
    """
    cfg = ctx.cfg
    out: dict[str, Any] = {
        "port":         0,
        "socks-port":   int(cfg.get("socks5_port", 0) or 0),
        "redir-port":   0,
        "tproxy-port":  0,
        "mixed-port":   0,
        "allow-lan":    False,
        "bind-address": str(cfg.get("socks5_host", "127.0.0.1")),
        "mode":         "rule",
        "log-level":    _derive_log_level(cfg),
        "ipv6":         True,
        # 透传 pyrealiy 自己的 cfg 摘要（脱敏后），便于面板"查看配置"
        "pyrealiy": _redact_cfg(cfg),
    }
    return json_response(out)


async def _proxies(req: Request, ctx: APIContext) -> Response:
    """Clash 格式 dict: { name → proxy_object }"""
    out: dict = {}
    if ctx.outbounds:
        for tag, o in ctx.outbounds.items():
            out[tag] = _outbound_to_clash(tag, o)
    return json_response({"proxies": out})


async def _proxy_one(req: Request, ctx: APIContext, name: str) -> Response:
    if not ctx.outbounds or name not in ctx.outbounds:
        return json_response({"code": 404, "message": "proxy not found"}, status=404)
    return json_response(_outbound_to_clash(name, ctx.outbounds[name]))


async def _rules(req: Request, ctx: APIContext) -> Response:
    """
    Clash 格式：
      { "rules": [ {"type":"DomainSuffix","payload":"google.com","proxy":"PROXY"} ] }
    末尾自动追加一条 Match 规则（pyrealiy 的 FINAL 不在 router._rules 里）。
    """
    rules: list[dict] = []
    if ctx.router_engine is not None:
        rules = list(ctx.router_engine.rules)
        # 显式追加 FINAL → "Match" 规则，让 UI 看到默认动作
        rules.append({
            "type":    "Match",
            "payload": "",
            "proxy":   ctx.router_engine.default,
            "invert":  False,
        })
    return json_response({"rules": rules})


async def _configs_reload(req: Request, ctx: APIContext) -> Response:
    """
    PUT /configs   Clash 兼容："重新加载配置文件"语义。

    范围与 SIGHUP 一致，见 core/reload.py 模块顶。
    locked field（outbounds / inbounds / api / schema_version）改动会在
    warnings 里报告，但不生效。
    """
    if ctx.reloader is None:
        return json_response({"code": 503, "message": "reload not available (no reloader)"}, status=503)
    result = await ctx.reloader.reload()
    if not result.get("ok"):
        return json_response({"code": 500, "message": result.get("error", "reload failed")}, status=500)
    return json_response(result)


async def _connections(req: Request, ctx: APIContext) -> Response:
    """
    Clash 格式：
      {
        "downloadTotal": int,
        "uploadTotal":   int,
        "memory":        int,            # 0（暂不实现）
        "connections":   [ ConnInfo.to_clash() ]
      }
    """
    if ctx.registry is None:
        return json_response({
            "downloadTotal": 0,
            "uploadTotal":   0,
            "memory":        0,
            "connections":   [],
        })
    meter = ctx.registry.meter
    return json_response({
        "downloadTotal": meter.down_total,
        "uploadTotal":   meter.up_total,
        "memory":        0,
        "connections":   [c.to_clash() for c in ctx.registry.list_active()],
    })


# ---------- Outbound → Clash 映射 ----------

# pyrealiy outbound 类型映射成 Clash 节点类型。
# 用 "Trojan" 让 UI 显示代理图标 + 可被 Clash 配置 import（行为最接近）。
_TYPE_MAP = {
    "pyrealiy": "Trojan",
    "direct":   "Direct",
    "block":    "Reject",
    "urltest":  "URLTest",
    "fallback": "Fallback",
}


def _outbound_to_clash(tag: str, o) -> dict:
    """单个 outbound → Clash UI 期望的 dict。"""
    clash_type = _TYPE_MAP.get(getattr(o, "type", "unknown"), "Direct")
    base: dict = {
        "type":    clash_type,
        "name":    tag,
        "udp":     True,           # pyrealiy + direct 都支持 UDP
        "alive":   bool(getattr(o, "is_healthy", True)),
        "history": _history_for(o),
        "extra":   {},             # Yacd 可选字段；这里留空
    }
    # 叶子节点：补 server / port
    if clash_type == "Trojan":
        try:
            base["server"] = o._pool._cfg.get("server_host", "")
            base["port"]   = int(o._pool._cfg.get("server_port", 0))
        except Exception:
            pass
    # 组节点：补 all / now
    if clash_type in ("URLTest", "Fallback"):
        children = getattr(o, "_children", []) or []
        base["all"] = [c.tag for c in children]
        # urltest 暴露 current 选择；fallback 取 resolve_leaf 的结果
        if clash_type == "URLTest":
            cur = getattr(o, "_current", None)
            base["now"] = cur.tag if cur else (children[0].tag if children else "")
        else:
            try:
                leaf = o.resolve_leaf()
                base["now"] = leaf.tag if leaf is not o else (children[0].tag if children else "")
            except Exception:
                base["now"] = children[0].tag if children else ""
    return base


def _history_for(o) -> list[dict]:
    """
    Clash history: [{time: ISO, delay: ms_int}]。
    pyrealiy 没存 per-sample 时间戳，只能给最近一次的 median + last_sample_time。
    Yacd 主要读 history[-1].delay 作"当前延迟"，给一条足够。
    """
    lat = getattr(o, "latency_ms", None)
    if lat is None:
        return []
    import time as _time
    last_age = getattr(o, "latency_age_sec", 0.0) or 0.0
    epoch = max(0.0, _time.time() - last_age)
    from .stats import _iso
    return [{"time": _iso(epoch), "delay": int(round(lat))}]


# ---------- 脱敏 ----------

def _derive_log_level(cfg: dict) -> str:
    """tuning.log_levels 优先；否则 log.level；都没有 → info"""
    tuning = cfg.get("tuning") or {}
    log_levels = tuning.get("log_levels") if isinstance(tuning, dict) else None
    if isinstance(log_levels, dict):
        # 取根 logger 级别
        root = log_levels.get("root")
        if root:
            return str(root).lower()
    log = cfg.get("log") or {}
    if isinstance(log, dict) and log.get("level"):
        return str(log["level"]).lower()
    return "info"


def _redact_cfg(cfg: dict) -> dict:
    """递归剔除敏感字段。生成新 dict，不动 cfg。"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if k in _SECRET_FIELDS_TOP:
            out[k] = "***"
            continue
        if k == "api":
            out[k] = _redact_dict(v, _SECRET_FIELDS_API) if isinstance(v, dict) else v
            continue
        if k == "outbounds" and isinstance(v, list):
            out[k] = [_redact_dict(o, _SECRET_FIELDS_OUTBOUND) if isinstance(o, dict) else o for o in v]
            continue
        out[k] = v
    return out


def _redact_dict(d: dict, fields: set[str]) -> dict:
    return {k: ("***" if k in fields and v else v) for k, v in d.items()}
