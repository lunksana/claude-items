"""
PyReality 配置 schema v1。

schema_version=1 顶层 8 个 key（合约：1 期间不再加）：
  schema_version  元字段
  log             日志
  inbounds        入站
  outbounds       出站
  route           路由
  dns             DNS（转发 + 缓存 + 分流 + hosts + fakeip）
  api             Clash 兼容 API
  tuning          高级调优（README 单独章节文档化）

向后兼容：
  - 无 schema_version + 命中 legacy 顶层 key → 视为 v0 legacy；启动期 INFO 日志，
    保持原有自动合成逻辑（outbound._synthesize_legacy_outbound 等）
  - schema_version=1 但缺可选 section → 不做填充（下游模块各自看到 missing 走默认）
  - 未知顶层 key → WARN（不阻止启动，便于发现拼写错误）

跨引用检查（dns.resolvers[*].via 必须存在于 outbounds.tag 集合）暂不在 load_config
里做：build_outbounds 之后用 validate_cross_refs(cfg, outbound_tags) 补一次。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("config")

SCHEMA_VERSION_CURRENT = 1

_V1_TOP_KEYS = {
    "schema_version", "log", "inbounds", "outbounds",
    "route", "dns", "api", "tuning",
}

_LEGACY_TOP_KEYS = {
    "server_host", "server_port", "password", "camouflage_host",
    "socks5_host", "socks5_port", "pool_size",
    "brutal_rate_bps", "brutal_pool_size",
}

_DNS_MATCH_PREFIXES = (
    "domain:", "domain-suffix:", "domain-keyword:", "domain-regex:",
    "geosite:", "ipcidr:", "geoip:",
)

_DNS_STRATEGIES = {"prefer_ipv4", "prefer_ipv6", "ipv4_only", "ipv6_only"}


class ConfigError(ValueError):
    """配置语法 / 必填字段 / 跨引用错误。启动期抛出后由 main 捕获并退出。"""


def load_config(path: str) -> dict:
    """读取、验证、规范化配置。返回 dict 直接交给下游 build_outbounds / build_router 等。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a JSON object, got {type(raw).__name__}")

    # 在校验前抢先切日志格式：让 load_config 自己的 INFO/WARN 也按 JSON 输出，
    # 否则首启动 2 行（dns.default 回退、tuning 检测）会卡在 text 而后续全 JSON
    try:
        from .utils import apply_log_format
        apply_log_format(raw)
    except Exception:
        pass  # 格式应用失败不能拦截配置加载本身

    schema_version = raw.get("schema_version")

    if schema_version is None:
        if _looks_like_legacy(raw):
            hit = sorted(set(raw.keys()) & _LEGACY_TOP_KEYS)
            logger.info(
                "Legacy schema detected (no schema_version; legacy keys present: %s). "
                "Auto-migrated in memory; downstream modules will synthesize v1 structure. "
                "To silence this notice, migrate the file to schema_version=1.",
                ", ".join(hit),
            )
            return raw
        logger.warning(
            "Config has no schema_version and no recognized legacy keys; "
            "treating as schema_version=1",
        )
        schema_version = SCHEMA_VERSION_CURRENT

    if schema_version != SCHEMA_VERSION_CURRENT:
        raise ConfigError(
            f"unsupported schema_version={schema_version}; "
            f"this build only supports schema_version={SCHEMA_VERSION_CURRENT}"
        )

    _warn_unknown_top_keys(raw)
    _validate_inbounds(raw.get("inbounds"))
    _validate_dns(raw.get("dns"))
    _validate_api(raw.get("api"))
    _apply_dns_defaults(raw)
    _project_v1_to_legacy_keys(raw)
    _log_tuning_overrides(raw.get("tuning"))
    return raw


def validate_cross_refs(cfg: dict, outbound_tags: set[str]) -> None:
    """build_outbounds 之后调用：检查 dns.resolvers[*].via 与 dns.default 都指到真实 outbound。"""
    dns = cfg.get("dns") or {}
    for i, r in enumerate(dns.get("resolvers", []) or []):
        via = r.get("via")
        if via and via not in outbound_tags:
            raise ConfigError(
                f"dns.resolvers[{i}]({r.get('tag')}): 'via'='{via}' does not match any outbound tag "
                f"(known tags: {sorted(outbound_tags)})"
            )


# ---------- internals ----------

def _looks_like_legacy(raw: dict) -> bool:
    return bool(set(raw.keys()) & _LEGACY_TOP_KEYS)


def _warn_unknown_top_keys(raw: dict) -> None:
    unknown = set(raw.keys()) - _V1_TOP_KEYS
    if unknown:
        logger.warning(
            "config: unknown top-level keys ignored: %s (schema_version=1 keys: %s)",
            sorted(unknown), sorted(_V1_TOP_KEYS - {"schema_version"}),
        )


def _validate_inbounds(inbounds: Any) -> None:
    if inbounds is None:
        return
    if not isinstance(inbounds, list):
        raise ConfigError("inbounds: must be a list")
    for i, ib in enumerate(inbounds):
        if not isinstance(ib, dict):
            raise ConfigError(f"inbounds[{i}]: must be an object")
        t = ib.get("type")
        if t not in ("socks5",):
            # 留出未来扩展位（http / mixed / tun），现在只识别 socks5
            raise ConfigError(
                f"inbounds[{i}]: type '{t}' not supported in this build (only 'socks5')"
            )
        listen = ib.get("listen")
        if not listen or not isinstance(listen, str) or ":" not in listen:
            raise ConfigError(f"inbounds[{i}]({t}): 'listen' must be 'host:port' string")


def _project_v1_to_legacy_keys(raw: dict) -> None:
    """
    把 v1 sections 投射到 client.py / dns_forwarder.py 仍直接读的"老顶层 key"上。

    这是过渡层：避免 P0 改 client.py / dns_forwarder.py 里的 cfg['socks5_host'] 等
    直接访问。等下游模块逐步迁移到读 inbounds/dns/api 子结构后，可以删除本函数。

    只投射缺失的字段；老格式（已有顶层 key）原样不动。
    """
    # inbounds[0] (socks5) → cfg['socks5_host'] / ['socks5_port']
    inbounds = raw.get("inbounds") or []
    for ib in inbounds:
        if ib.get("type") != "socks5":
            continue
        host, port_s = ib["listen"].rsplit(":", 1)
        raw.setdefault("socks5_host", host)
        raw.setdefault("socks5_port", int(port_s))
        break

    # dns.listen → cfg['dns_listen_host'] / ['dns_listen_port']
    dns_listen = (raw.get("dns") or {}).get("listen")
    if dns_listen and ":" in dns_listen:
        host, port_s = dns_listen.rsplit(":", 1)
        raw.setdefault("dns_listen_host", host)
        raw.setdefault("dns_listen_port", int(port_s))


def _validate_dns(dns: Any) -> None:
    if dns is None or dns == {}:
        return
    if not isinstance(dns, dict):
        raise ConfigError("dns: must be an object")

    resolvers = dns.get("resolvers", [])
    if not isinstance(resolvers, list):
        raise ConfigError("dns.resolvers: must be a list")

    tags: set[str] = set()
    for i, r in enumerate(resolvers):
        if not isinstance(r, dict):
            raise ConfigError(f"dns.resolvers[{i}]: must be an object")
        tag = r.get("tag")
        if not tag or not isinstance(tag, str):
            raise ConfigError(f"dns.resolvers[{i}]: 'tag' required (string)")
        if tag in tags:
            raise ConfigError(f"dns.resolvers[{i}]: duplicate tag '{tag}'")
        tags.add(tag)
        if not r.get("address") or not isinstance(r["address"], str):
            raise ConfigError(f"dns.resolvers[{i}]({tag}): 'address' required (string)")
        via = r.get("via")
        if not via or not isinstance(via, str):
            raise ConfigError(
                f"dns.resolvers[{i}]({tag}): 'via' required (outbound tag, string)"
            )

    default = dns.get("default")
    if default is not None:
        if not isinstance(default, str):
            raise ConfigError("dns.default: must be a string (resolver tag)")
        if default not in tags:
            raise ConfigError(
                f"dns.default: tag '{default}' not in dns.resolvers (known: {sorted(tags)})"
            )

    rules = dns.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError("dns.rules: must be a list")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ConfigError(f"dns.rules[{i}]: must be an object")
        match = rule.get("match")
        use = rule.get("use")
        if not match or not isinstance(match, str):
            raise ConfigError(f"dns.rules[{i}]: 'match' required")
        if not any(match.startswith(p) for p in _DNS_MATCH_PREFIXES):
            raise ConfigError(
                f"dns.rules[{i}]: 'match' must start with one of {_DNS_MATCH_PREFIXES}"
            )
        if not use or not isinstance(use, str):
            raise ConfigError(f"dns.rules[{i}]: 'use' required (resolver tag)")
        if use not in tags:
            raise ConfigError(
                f"dns.rules[{i}]: 'use'='{use}' not in dns.resolvers (known: {sorted(tags)})"
            )

    strategy = dns.get("strategy")
    if strategy is not None and strategy not in _DNS_STRATEGIES:
        raise ConfigError(
            f"dns.strategy: must be one of {sorted(_DNS_STRATEGIES)}, got '{strategy}'"
        )

    for k in ("cache", "hosts", "fakeip"):
        sub = dns.get(k)
        if sub is not None and not isinstance(sub, dict):
            raise ConfigError(f"dns.{k}: must be an object")


def _apply_dns_defaults(raw: dict) -> None:
    """dns.default 未填 → 取 dns.resolvers[0].tag。空 resolvers 不动。"""
    dns = raw.get("dns")
    if not dns or dns.get("default"):
        return
    resolvers = dns.get("resolvers") or []
    if resolvers:
        dns["default"] = resolvers[0]["tag"]
        logger.info("dns.default not set; using first resolver '%s'", dns["default"])


def _validate_api(api: Any) -> None:
    if api is None or api == {}:
        return
    if not isinstance(api, dict):
        raise ConfigError("api: must be an object")
    listen = api.get("listen")
    if not listen:
        return  # 未启用 API
    if not isinstance(listen, str) or ":" not in listen:
        raise ConfigError("api.listen: must be a 'host:port' string")
    host = listen.rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "api.listen binds to %s — Clash API has only Bearer-token auth; "
            "exposing to non-loopback is risky", host,
        )
    secret = api.get("secret")
    if not secret or not isinstance(secret, str):
        raise ConfigError("api.secret: required (non-empty string) when api.listen is set")


def _log_tuning_overrides(tuning: Any) -> None:
    if not tuning:
        return
    if not isinstance(tuning, dict):
        raise ConfigError("tuning: must be an object")
    logger.info("tuning overrides active: %s", sorted(tuning.keys()))
