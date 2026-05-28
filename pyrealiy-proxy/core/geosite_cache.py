"""
Geo 数据缓存管理器（geosite + geoip 统一管理）

缓存目录结构（默认 .geosite/）：
  .geosite/
    meta.json              ← 轻量元数据：各源的 URL 和下载时间
    site-loyalsoldier.dat  ← geosite 源（site- 前缀）
    site-v2fly.dat
    ip-loyalsoldier.dat    ← geoip  源（ip-  前缀）

meta.json 格式：
  {
    "v": 1,
    "sources": {
      "site-loyalsoldier": { "url": "...", "downloaded_at": 1700000000 },
      "ip-loyalsoldier":   { "url": "...", "downloaded_at": 1700000001 }
    }
  }

config_client.json 配置示例：
  {
    "geosite_dir":         ".geosite",
    "geosite_update_days": 7,
    "geosite_sources": [
      { "name": "loyalsoldier",
        "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat" },
      { "name": "v2fly",
        "url":  "https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat",
        "update_days": 3 }
    ],
    "geoip_sources": [
      { "name": "loyalsoldier",
        "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat" }
    ]
  }
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request

from .utils import get_logger

logger = get_logger("geo_cache")

_META_VERSION = 1
_META_FILE    = "meta.json"


# ── 元数据读写 ─────────────────────────────────────────────────────────────────

def _read_meta(cache_dir: str) -> dict:
    path = os.path.join(cache_dir, _META_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == _META_VERSION:
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"v": _META_VERSION, "sources": {}}


def _write_meta(cache_dir: str, meta: dict) -> None:
    path = os.path.join(cache_dir, _META_FILE)
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)


# ── 单源下载 ───────────────────────────────────────────────────────────────────

def _dat_path(cache_dir: str, key: str) -> str:
    """key = "site-loyalsoldier" 或 "ip-loyalsoldier" """
    return os.path.join(cache_dir, f"{key}.dat")


def _download(url: str, dest: str) -> None:
    """同步下载（在线程池中调用），原子写入"""
    tmp = dest + ".tmp"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pyrealiy-proxy/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(65536):
                f.write(chunk)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


async def _ensure_one(
    key: str,
    url: str,
    cache_dir: str,
    update_days: float,
    meta: dict,
) -> bool:
    dest             = _dat_path(cache_dir, key)
    downloaded_at    = meta["sources"].get(key, {}).get("downloaded_at", 0)
    age_days         = (time.time() - downloaded_at) / 86400
    needs_download   = not os.path.exists(dest) or age_days > update_days

    if not needs_download:
        logger.debug("geo[%s] up-to-date (%.1f days old)", key, age_days)
        return True

    logger.info("Downloading geo[%s] from %s ...", key, url)
    try:
        await asyncio.to_thread(_download, url, dest)
        meta["sources"][key] = {"url": url, "downloaded_at": int(time.time())}
        logger.info("geo[%s] saved (%d KB)", key, os.path.getsize(dest) // 1024)
        return True
    except Exception as e:
        logger.warning("geo[%s] download failed: %s", key, e)
        return os.path.exists(dest)   # 降级：仍有旧文件则继续用


# ── 公共入口 ───────────────────────────────────────────────────────────────────

async def _ensure_typed(
    type_prefix: str,                  # "site" 或 "ip"
    sources: list[dict],
    cache_dir: str,
    default_update_days: float,
    meta: dict,
) -> dict[str, str]:
    """并发确保一组同类型源可用，返回 {source_name: dat_path}"""
    # 先过滤出有效条目，tasks 与 valid 一一对应；zip 必须用过滤后的列表，
    # 否则一个坏条目会让后续 source 与 result 错位（已修复 bug）
    valid = [s for s in sources if s.get("name") and s.get("url")]
    tasks = [
        _ensure_one(
            key         = f"{type_prefix}-{s['name']}",
            url         = s["url"],
            cache_dir   = cache_dir,
            update_days = s.get("update_days", default_update_days),
            meta        = meta,
        )
        for s in valid
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        s["name"]: _dat_path(cache_dir, f"{type_prefix}-{s['name']}")
        for s, ok in zip(valid, results)
        if ok is True
    }


async def ensure_all(cfg: dict) -> tuple[dict[str, str], dict[str, str]]:
    """
    并发下载/刷新所有 geosite 和 geoip 源。
    返回 (site_paths, ip_paths)，均为 {source_name: dat_path}。

    兼容旧版单源字段 geosite_url / geosite_path：
      自动转换为名为 "default" 的 geosite 源。
    """
    cache_dir    = cfg.get("geosite_dir", ".geosite")
    default_days = cfg.get("geosite_update_days", 7)

    # 兼容旧版单源配置
    site_sources = cfg.get("geosite_sources") or []
    if not site_sources:
        url  = cfg.get("geosite_url", "")
        path = cfg.get("geosite_path", "")
        if path and not url and os.path.exists(path):
            # 旧版本地文件直接使用，不纳入缓存目录
            site_sources = []
            site_fallback = {"default": path}
        elif url:
            site_sources = [{"name": "default", "url": url}]
            site_fallback = {}
        else:
            site_fallback = {}
    else:
        site_fallback = {}

    ip_sources = cfg.get("geoip_sources") or []

    os.makedirs(cache_dir, exist_ok=True)
    meta = _read_meta(cache_dir)

    # geosite 和 geoip 并发下载
    site_task = _ensure_typed("site", site_sources, cache_dir, default_days, meta)
    ip_task   = _ensure_typed("ip",   ip_sources,   cache_dir, default_days, meta)
    site_paths, ip_paths = await asyncio.gather(site_task, ip_task)

    site_paths.update(site_fallback)
    _write_meta(cache_dir, meta)

    if site_paths:
        logger.info("geosite ready: %s", list(site_paths))
    if ip_paths:
        logger.info("geoip   ready: %s", list(ip_paths))

    return site_paths, ip_paths
