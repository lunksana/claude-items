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
import ssl
import time
import urllib.parse
import urllib.request

from .utils import get_logger, pack_address

logger = get_logger("geo_cache")

_META_VERSION = 1
_META_FILE    = "meta.json"

# 隧道下载相关
_TUNNEL_MAX_REDIRECTS = 5
_TUNNEL_HS_TIMEOUT    = 15.0   # TLS 握手单次 recv 等待
_TUNNEL_BODY_TIMEOUT  = 60.0   # 响应体单次 recv 等待


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
    """同步直连下载（在线程池中调用），原子写入。tunnel 路径失败时的兜底。"""
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


# ── 隧道下载（优先路径）─────────────────────────────────────────────────────────

async def _download_via_tunnel(url: str, dest: str, pool) -> None:
    """
    经我们自己的加密隧道下载：pool 拿一条 tunnel → server 为我们连 host:443 →
    在 tunnel 上跑真实 TLS（ssl.MemoryBIO 模式）到 GitHub → 普通 HTTP/1.1 GET。

    这样弱网下不依赖宿主机直连 GitHub 的成功率，借用 VPS 出口带宽。
    """
    body = await _https_get_via_pool(url, pool)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


async def _https_get_via_pool(url: str, pool) -> bytes:
    for _ in range(_TUNNEL_MAX_REDIRECTS + 1):
        u = urllib.parse.urlparse(url)
        if u.scheme != "https":
            raise ValueError(f"only https supported, got {u.scheme!r}")
        host = u.hostname or ""
        port = u.port or 443
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")

        status, headers, body = await _https_request_once(host, port, path, pool)

        if 300 <= status < 400:
            loc = next((v for k, v in headers if k.lower() == "location"), None)
            if not loc:
                raise IOError(f"redirect {status} missing Location")
            url = loc if loc.startswith(("http://", "https://")) else f"https://{host}{loc}"
            logger.debug("geo redirect %d -> %s", status, url)
            continue
        if status != 200:
            raise IOError(f"HTTP {status}")
        return body
    raise IOError(f"too many redirects ({_TUNNEL_MAX_REDIRECTS})")


async def _https_request_once(
    host: str, port: int, path: str, pool,
) -> tuple[int, list[tuple[str, str]], bytes]:
    ready = await pool.acquire()
    if ready is None:
        raise OSError("no tunnel available")
    tunnel = ready.tunnel
    try:
        # 服务端按这条地址 dial 目标主机
        await tunnel.send(pack_address(host, port))

        # MemoryBIO：incoming 喂网络数据给 TLS，outgoing 收 TLS 要发的数据
        ctx = ssl.create_default_context()
        incoming = ssl.MemoryBIO()
        outgoing = ssl.MemoryBIO()
        sslobj   = ctx.wrap_bio(incoming, outgoing, server_hostname=host)

        async def flush() -> None:
            data = outgoing.read()
            if data:
                await tunnel.send(data)

        # 跟踪是否收到过任何对端字节，握手阶段全 EOF 时给出针对性诊断
        received_any = [False]

        async def feed(timeout: float) -> bool:
            """
            从隧道喂数据给 TLS。返回 False 表示"没有更多数据"——超时或对端关闭都算。
            对端关闭并不必然是错误：HTTP/1.1 Connection: close 的末尾本来就是 EOF，
            上层会根据 _parse_http_response + Content-Length 判断响应是否完整。
            """
            try:
                data = await asyncio.wait_for(tunnel.recv(), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return False
            if not data:
                return False
            received_any[0] = True
            incoming.write(data)
            return True

        # TLS 握手：握手期间断开是硬错误，需要明确报错
        while True:
            try:
                sslobj.do_handshake()
                break
            except ssl.SSLWantReadError:
                await flush()
                if not await feed(_TUNNEL_HS_TIMEOUT):
                    if not received_any[0]:
                        raise IOError(
                            f"server closed tunnel before any data for {host}:{port} "
                            f"— VPS likely can't reach the target "
                            f"(check server logs for 'Cannot connect to {host}:{port}')"
                        )
                    raise IOError(f"TLS handshake aborted to {host}:{port}")
            except ssl.SSLWantWriteError:
                await flush()
        await flush()

        # HTTP GET
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: pyrealiy-proxy/1.0\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        sslobj.write(req)
        await flush()

        # 读到对端 close
        raw = bytearray()
        while True:
            try:
                chunk = sslobj.read(65536)
            except ssl.SSLWantReadError:
                if not await feed(_TUNNEL_BODY_TIMEOUT):
                    break
                continue
            except ssl.SSLZeroReturnError:
                break
            if not chunk:
                if not await feed(_TUNNEL_BODY_TIMEOUT):
                    break
                continue
            raw.extend(chunk)

        return _parse_http_response(bytes(raw))
    finally:
        ready.close()


def _parse_http_response(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    end = raw.find(b"\r\n\r\n")
    if end < 0:
        raise IOError("malformed response: no header terminator (body too short)")
    head = raw[:end].decode("latin-1")
    body = raw[end + 4:]

    lines = head.split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        raise IOError(f"bad status line: {lines[0]!r}")

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))

    is_chunked = any(k.lower() == "transfer-encoding" and "chunked" in v.lower()
                     for k, v in headers)
    cl_str = next((v for k, v in headers if k.lower() == "content-length"), None)

    if is_chunked:
        body = _dechunk(body)
    elif cl_str is not None and cl_str.strip().isdigit():
        expected = int(cl_str.strip())
        if len(body) < expected:
            raise IOError(
                f"truncated body: got {len(body)} of {expected} bytes "
                f"(tunnel closed before full response received)"
            )
        body = body[:expected]   # 去掉 Content-Length 之外多读的字节

    return status, headers, body


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(data):
        nl = data.find(b"\r\n", pos)
        if nl < 0:
            break
        try:
            size = int(data[pos:nl].split(b";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        pos = nl + 2
        out.extend(data[pos:pos + size])
        pos += size + 2
    return bytes(out)


async def _ensure_one(
    key: str,
    url,                             # str 或 list[str]：支持多镜像 fallback
    cache_dir: str,
    update_days: float,
    meta: dict,
    pool=None,                       # 给定则每个 URL 都先试隧道再走直连
) -> bool:
    urls = [url] if isinstance(url, str) else [u for u in url if u]
    if not urls:
        return False

    dest             = _dat_path(cache_dir, key)
    downloaded_at    = meta["sources"].get(key, {}).get("downloaded_at", 0)
    age_days         = (time.time() - downloaded_at) / 86400
    needs_download   = not os.path.exists(dest) or age_days > update_days

    if not needs_download:
        logger.debug("geo[%s] up-to-date (%.1f days old)", key, age_days)
        return True

    logger.info("Downloading geo[%s] from %d mirror(s) ...", key, len(urls))

    for i, u in enumerate(urls, start=1):
        # 优先：经我们自己的加密隧道（弱网更稳）
        if pool is not None:
            try:
                await _download_via_tunnel(u, dest, pool)
                meta["sources"][key] = {"url": u, "downloaded_at": int(time.time())}
                logger.info("geo[%s] saved via tunnel (mirror %d/%d, %d KB): %s",
                            key, i, len(urls), os.path.getsize(dest) // 1024, u)
                return True
            except Exception as e:
                logger.warning("geo[%s] tunnel mirror %d/%d failed: %s",
                               key, i, len(urls), e)

        # 兜底：直连
        try:
            await asyncio.to_thread(_download, u, dest)
            meta["sources"][key] = {"url": u, "downloaded_at": int(time.time())}
            logger.info("geo[%s] saved direct (mirror %d/%d, %d KB): %s",
                        key, i, len(urls), os.path.getsize(dest) // 1024, u)
            return True
        except Exception as e:
            logger.warning("geo[%s] direct mirror %d/%d failed: %s",
                           key, i, len(urls), e)

    logger.warning("geo[%s] all %d mirrors failed; using cached file if any",
                   key, len(urls))
    return os.path.exists(dest)


# ── 公共入口 ───────────────────────────────────────────────────────────────────

async def _ensure_typed(
    type_prefix: str,                  # "site" 或 "ip"
    sources: list[dict],
    cache_dir: str,
    default_update_days: float,
    meta: dict,
    pool=None,
) -> dict[str, str]:
    """并发确保一组同类型源可用，返回 {source_name: dat_path}"""
    # 先过滤出有效条目，tasks 与 valid 一一对应；zip 必须用过滤后的列表，
    # 否则一个坏条目会让后续 source 与 result 错位（已修复 bug）
    def _has_url(s):
        u = s.get("url")
        return bool(u if isinstance(u, str) else (u and any(u)))
    valid = [s for s in sources if s.get("name") and _has_url(s)]
    tasks = [
        _ensure_one(
            key         = f"{type_prefix}-{s['name']}",
            url         = s["url"],
            cache_dir   = cache_dir,
            update_days = s.get("update_days", default_update_days),
            meta        = meta,
            pool        = pool,
        )
        for s in valid
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        s["name"]: _dat_path(cache_dir, f"{type_prefix}-{s['name']}")
        for s, ok in zip(valid, results)
        if ok is True
    }


async def ensure_all(cfg: dict, pool=None) -> tuple[dict[str, str], dict[str, str]]:
    """
    并发下载/刷新所有 geosite 和 geoip 源。
    返回 (site_paths, ip_paths)，均为 {source_name: dat_path}。

    给定 pool 时，下载优先经我们自己的加密隧道（弱网下比直连 GitHub 稳）；
    隧道失败自动 fallback 直连。

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
    site_task = _ensure_typed("site", site_sources, cache_dir, default_days, meta, pool)
    ip_task   = _ensure_typed("ip",   ip_sources,   cache_dir, default_days, meta, pool)
    site_paths, ip_paths = await asyncio.gather(site_task, ip_task)

    site_paths.update(site_fallback)
    _write_meta(cache_dir, meta)

    if site_paths:
        logger.info("geosite ready: %s", list(site_paths))
    if ip_paths:
        logger.info("geoip   ready: %s", list(ip_paths))

    return site_paths, ip_paths
