"""
配置热加载协调器。

可热加载范围（schema_version=1 期间锁定）：
  ✓ route.rules / route.default / route.final / rules / final
  ✓ dns_listen_host / dns_listen_port 内部 routing（不重 bind socket，只换转发逻辑）
  ✓ cn_dns / remote_dns（DNS forwarder 重新 build upstream）
  ✓ log.format / log_levels
  ✓ tuning.access_log / tuning.routing_cache.ttl_sec 等运行时可调项

不动（reload 不影响这些，新值警告并保留旧值）：
  ✗ outbounds（pool TCP 长连接 + 握手 + Brutal 速率状态）
  ✗ inbounds（socket 已 bind）
  ✗ api.listen / api.secret（API server 已起）
  ✗ schema_version

触发：
  - SIGHUP 信号（systemd reload 友好）
  - POST /configs（Clash 兼容；只在 api.listen 配置时可用）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import load_config

logger = logging.getLogger("reload")

# 不可热加载字段；reload 时若新值与旧值不同，记录 WARN 并保留旧值
_LOCKED_TOP = ("schema_version", "inbounds", "outbounds", "api")
_LOCKED_LEGACY = ("socks5_host", "socks5_port", "server_host", "server_port", "password")


class RouterRef:
    """
    Router 的 1-级间接引用。热加载时整体替换 _inner。
    所有 _dispatch / DNSForwarder 都通过此 ref 调，**绝不**直接持有 inner。
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    @property
    def inner(self):
        return self._inner

    def replace(self, new_inner) -> None:
        self._inner = new_inner

    # 透传 Router 接口
    def match(self, host: str):
        return self._inner.match(host)

    @property
    def default(self) -> str:
        return self._inner.default

    @property
    def rules(self):
        return self._inner.rules


class Reloader:
    """
    持有所有"可热加载子系统"的引用 + 当前 cfg。reload(new_cfg_path) 协调更新顺序。
    """

    def __init__(self, cfg_path: str, cfg: dict):
        self.cfg_path = cfg_path
        self.cfg = cfg
        # 下列引用由 main() 设置；reload 期间会用
        self.router_ref: Optional[RouterRef] = None
        self.outbounds: Optional[dict] = None
        self.routing_cache = None
        self.dns_cache = None
        self.dns_forwarder = None
        # API context：reload 成功后把 api_ctx.cfg 也同步成新 cfg，
        # 否则 GET /configs 一直返回老快照（B1）
        self.api_ctx = None
        # geo 数据在启动期下载一次，reload 不重下；保留下来给 rebuild router 用
        self.available_site: Optional[dict] = None
        self.available_ip: Optional[dict] = None
        self.legacy_action_map: Optional[dict] = None
        # 由调用方注册的"new cfg 来了之后该做什么"回调
        # 比如 client.py 注册一个把 access_log 重新读到 _ACCESS_LOG 的回调
        self.tuning_handlers: list = []
        # 防并发 reload
        self._lock = asyncio.Lock()

    def add_tuning_handler(self, fn) -> None:
        """注册一个 fn(new_cfg)，reload 时被调以同步运行时可调项（如 access_log）。"""
        self.tuning_handlers.append(fn)

    async def reload(self) -> dict:
        """
        从 cfg_path 重读配置并应用可热加载的部分。返回操作摘要 dict（给 API 回包用）。
        失败时尽量回滚到旧 cfg。
        """
        async with self._lock:
            try:
                new_cfg = load_config(self.cfg_path)
            except Exception as e:
                logger.error("reload: failed to load config: %s", e)
                return {"ok": False, "error": f"load failed: {e}", "changed": []}

            locked_diffs = self._detect_locked_diffs(self.cfg, new_cfg)
            changed: list[str] = []
            warnings: list[str] = []

            # 1) 日志（先做，让后续 reload 日志按新设置走）
            try:
                from .utils import apply_log_format, apply_log_levels
                apply_log_format(new_cfg)
                apply_log_levels(new_cfg)
                changed.append("log")
            except Exception as e:
                warnings.append(f"log reload skipped: {e}")

            # 2) 注册的 tuning 回调（access_log 等运行时可调项）
            for fn in self.tuning_handlers:
                try:
                    fn(new_cfg)
                except Exception as e:
                    warnings.append(f"tuning handler {fn.__name__} failed: {e}")
            if self.tuning_handlers:
                changed.append("tuning")

            # 3) Router：rebuild + 原子替换；同时 invalidate 两个缓存
            #    routing_cache：规则改了，老的 domain→outbound 决策可能错
            #    dns_cache（B2）：规则改后，缓存里的 IP 来自老路由的上游（如本地 DNS），
            #    必须清，否则下次同名查询用老 IP 走新规则的目标 outbound，行为错乱
            try:
                if self.router_ref is not None and self.outbounds is not None:
                    from .router import build_router
                    valid_actions = set(self.outbounds.keys())
                    new_router = build_router(
                        new_cfg,
                        self.available_site or {},
                        self.available_ip or {},
                        valid_actions=valid_actions,
                        legacy_action_map=self.legacy_action_map or {},
                    )
                    self.router_ref.replace(new_router)
                    rc_n = self.routing_cache.invalidate() if self.routing_cache is not None else 0
                    dc_n = self.dns_cache.invalidate() if self.dns_cache is not None else 0
                    changed.append(f"router (routing_cache cleared {rc_n}, dns_cache cleared {dc_n})")
            except Exception as e:
                warnings.append(f"router reload failed: {e}")

            # 4) DNS forwarder：换 remote_dns / cn_dns，drop _tunnels
            try:
                if self.dns_forwarder is not None:
                    self.dns_forwarder.reload(new_cfg)
                    changed.append("dns_forwarder")
            except Exception as e:
                warnings.append(f"dns_forwarder reload failed: {e}")

            # 提交
            self.cfg = new_cfg
            # B1：同步 api_ctx.cfg，不然 GET /configs 永远返回老快照
            if self.api_ctx is not None:
                try:
                    self.api_ctx.cfg = new_cfg
                except Exception as e:
                    warnings.append(f"api_ctx.cfg sync failed: {e}")

            if locked_diffs:
                for k in locked_diffs:
                    logger.warning("reload: locked field '%s' changed; restart required to take effect", k)
                warnings.extend([f"locked: {k}" for k in locked_diffs])

            logger.info("reload: applied changes=%s warnings=%s", changed, warnings)
            return {
                "ok":       True,
                "changed":  changed,
                "warnings": warnings,
            }

    @staticmethod
    def _detect_locked_diffs(old: dict, new: dict) -> list[str]:
        out = []
        for k in _LOCKED_TOP + _LOCKED_LEGACY:
            if old.get(k) != new.get(k):
                out.append(k)
        return out
