"""
pyrealiy 私有端点（/pyrealiy/*）。Clash 标准之外的诊断数据，给"pyrealiy 面板"
或 curl 排查用。Yacd 不读这些，不影响兼容性。

  GET /pyrealiy/pool       —— 每个 outbound 的 BrutalPool 实时状态
  GET /pyrealiy/timesync   —— 当前 offset / 上次同步源 / 漂移
  GET /pyrealiy/geo        —— geosite/geoip 缓存元数据（来自 meta.json）
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .http_proto import Request, Response
from .router import Router
from .server import APIContext, json_response


def register(router: Router, ctx: APIContext) -> None:
    router.add("GET", "/pyrealiy/pool",     _pool)
    router.add("GET", "/pyrealiy/timesync", _timesync)
    router.add("GET", "/pyrealiy/geo",      _geo)
    router.add("GET", "/pyrealiy/cache",    _cache)


# ============================================================
# /pyrealiy/pool
# ============================================================

async def _pool(req: Request, ctx: APIContext) -> Response:
    """
    每个 pyrealiy 出口的池实时快照：
      {
        "<outbound_tag>": {
          "ready":               int,    // 队列里就绪的隧道数
          "building":            int,    // 正在建立中的数量
          "target":              int,    // brutal_pool_size
          "next_build_in_sec":   float,  // 距下一条 build 真实起跑还有多久（staircase 游标）
          "stagger_step_sec":    float,
          "latency_ms":          float | null,
          "latency_age_sec":     float,
          "healthy":             bool,
          "consecutive_failures": int
        },
        ...
      }
    """
    out: dict[str, Any] = {}
    if not ctx.outbounds:
        return json_response(out)

    now_mono = time.monotonic()
    for tag, o in ctx.outbounds.items():
        # 只有 pyrealiy 类型才有 _pool
        pool = getattr(o, "_pool", None)
        if pool is None:
            continue

        next_build_at = float(getattr(pool, "_next_build_at", 0.0))
        next_in = max(0.0, next_build_at - now_mono) if next_build_at > 0 else 0.0

        lat = getattr(o, "latency_ms", None)
        age = getattr(o, "latency_age_sec", float("inf"))
        if age == float("inf"):
            age_view: Any = None
        else:
            age_view = round(float(age), 2)

        out[tag] = {
            "ready":                 int(pool.ready_count),
            "building":              int(getattr(pool, "_building", 0)),
            "target":                int(getattr(pool, "_pool_size", 0)),
            "next_build_in_sec":     round(next_in, 3),
            "stagger_step_sec":      float(getattr(pool, "_stagger_step", 0.0)),
            "latency_ms":            None if lat is None else round(float(lat), 1),
            "latency_age_sec":       age_view,
            "healthy":               bool(getattr(o, "is_healthy", True)),
            "consecutive_failures":  int(getattr(o, "_consecutive_failures", 0)),
        }
    return json_response(out)


# ============================================================
# /pyrealiy/timesync
# ============================================================

async def _timesync(req: Request, ctx: APIContext) -> Response:
    """
    {
      "offset_sec":        float,   // 当前 offset（local + offset = 真实时间）
      "last_source":       "ntp" | "https" | "",
      "last_sync_epoch":   float | null,  // unix 时间戳
      "last_sample_count": int,
      "max_offset_sec":    float,   // 配置上限（>1d 视为污染）
      "since_sync_sec":    float | null
    }
    """
    from core.time_sync import TimeSync
    last_at = float(TimeSync._last_sync_at_epoch)
    since = (time.time() - last_at) if last_at > 0 else None

    cfg_ts = (ctx.cfg.get("time_sync") or {}) if isinstance(ctx.cfg, dict) else {}
    max_off = float(cfg_ts.get("max_offset_sec", 86400))

    return json_response({
        "offset_sec":        round(TimeSync.get_offset(), 3),
        "last_source":       TimeSync._last_source,
        "last_sync_epoch":   round(last_at, 0) if last_at > 0 else None,
        "last_sample_count": int(TimeSync._last_sample_count),
        "max_offset_sec":    max_off,
        "since_sync_sec":    round(since, 1) if since is not None else None,
    })


# ============================================================
# /pyrealiy/cache
# ============================================================

async def _cache(req: Request, ctx: APIContext) -> Response:
    """
    {
      "routing": {"entries": N, "hits": ..., "misses": ..., "hit_rate": 0.95, "ttl_sec": 3600},
      "dns":     {"entries": N, "hits": ..., "misses": ..., "hit_rate": 0.85}
    }
    缓存未启用时对应字段为 null。
    """
    def with_rate(stats: dict) -> dict:
        h = stats.get("hits", 0)
        m = stats.get("misses", 0)
        total = h + m
        stats["hit_rate"] = round(h / total, 4) if total else 0.0
        return stats

    out = {}
    out["routing"] = with_rate(ctx.routing_cache.stats()) if ctx.routing_cache else None
    out["dns"] = with_rate(ctx.dns_cache.stats()) if ctx.dns_cache else None
    return json_response(out)


# ============================================================
# /pyrealiy/geo
# ============================================================

async def _geo(req: Request, ctx: APIContext) -> Response:
    """
    geosite/geoip 缓存视图，读 meta.json 拼配源信息。

    {
      "cache_dir":     "/opt/proxy/.geosite",
      "update_days":   7.0,
      "sources": [
        { "key": "site-loyalsoldier",
          "file_path": ".../site-loyalsoldier.dat",
          "exists":    true,
          "file_size": 1234567,
          "downloaded_epoch": 1700000000,
          "age_days":  1.3,
          "url":       "https://..." }
      ]
    }
    """
    cfg = ctx.cfg if isinstance(ctx.cfg, dict) else {}
    cache_dir_raw = cfg.get("geosite_dir") or ".geosite"
    cache_dir = os.path.abspath(cache_dir_raw)
    update_days = float(cfg.get("geosite_update_days", 7.0))

    meta_path = os.path.join(cache_dir, "meta.json")
    sources_view: list[dict] = []
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        sources = meta.get("sources", {}) or {}
        now = time.time()
        for key, info in sources.items():
            dat = os.path.join(cache_dir, f"{key}.dat")
            exists = os.path.isfile(dat)
            size = os.path.getsize(dat) if exists else 0
            dl_at = float(info.get("downloaded_at", 0)) if isinstance(info, dict) else 0.0
            age_days = round((now - dl_at) / 86400, 2) if dl_at > 0 else None
            sources_view.append({
                "key":              key,
                "file_path":        dat,
                "exists":           exists,
                "file_size":        int(size),
                "downloaded_epoch": int(dl_at) if dl_at > 0 else None,
                "age_days":         age_days,
                "url":              info.get("url", "") if isinstance(info, dict) else "",
            })
    except FileNotFoundError:
        pass
    except Exception:
        # meta 损坏不该让 API 挂掉
        pass

    return json_response({
        "cache_dir":   cache_dir,
        "update_days": update_days,
        "sources":     sources_view,
    })
