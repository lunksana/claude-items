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

# 下载完整性 / sanity
_MIN_DAT_SIZE = 100 * 1024       # 真实 geosite/geoip dat 至少几 MB；< 100KB 几乎一定是
                                 # HTML 错误页 / 0 字节 / 中途断流的残片，拒绝覆盖现有文件

# 隧道下载相关
_TUNNEL_MAX_REDIRECTS  = 5
_TUNNEL_HS_TIMEOUT     = 15.0    # TLS 握手单次 recv 等待
_TUNNEL_BODY_TIMEOUT   = 60.0    # 响应体单次 recv 等待
_TUNNEL_REQUEST_BUDGET = 180.0   # 单次 GET（含握手+全部 body）总时长上界，防慢速 DoS


def _check_dat_sane(path: str) -> None:
    """写入前最后一道 sanity check：拒绝异常小的文件。"""
    size = os.path.getsize(path)
    if size < _MIN_DAT_SIZE:
        raise IOError(
            f"suspiciously small dat: {size} bytes (< {_MIN_DAT_SIZE}); "
            f"likely an HTML error page or truncated body"
        )


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


def _download(url: str, dest: str,
              cached_etag: str | None = None,
              cached_lm:   str | None = None) -> dict:
    """
    同步直连下载（在线程池中调用），原子写入。tunnel 路径失败时的兜底。

    用显式空 ProxyHandler 屏蔽宿主机的 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 环境变量 ——
    否则用户从旧代理迁移过来若没清 env，fallback 会被劫持到死掉的旧代理，
    日志只会看到 "direct download failed" 但说不清根因。

    含 cached_etag / cached_lm 时发条件请求；上游 304 → 不重下、返回 not_modified。

    返回：
      {"kind": "saved" | "not_modified", "etag": str|None, "last_modified": str|None}
    """
    tmp = dest + ".tmp"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {"User-Agent": "mirage-proxy/1.0"}
        if cached_etag:
            headers["If-None-Match"] = cached_etag
        if cached_lm:
            headers["If-Modified-Since"] = cached_lm
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=60) as resp:
                new_etag = resp.headers.get("ETag")
                new_lm   = resp.headers.get("Last-Modified")
                cl       = resp.headers.get("Content-Length")
                expected = int(cl) if cl and cl.isdigit() else None
                written  = 0
                with open(tmp, "wb") as f:
                    while chunk := resp.read(65536):
                        f.write(chunk)
                        written += len(chunk)
                if expected is not None and written < expected:
                    raise IOError(f"truncated body: got {written} of {expected} bytes")
                _check_dat_sane(tmp)
                os.replace(tmp, dest)
                return {"kind": "saved", "etag": new_etag, "last_modified": new_lm}
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return {"kind": "not_modified",
                        "etag": cached_etag, "last_modified": cached_lm}
            raise
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── 隧道下载（优先路径）─────────────────────────────────────────────────────────

async def _download_via_tunnel(
    url: str, dest: str, pool,
    cached_etag: str | None = None,
    cached_lm:   str | None = None,
) -> dict:
    """
    经我们自己的加密隧道下载：pool 拿一条 tunnel → server 为我们连 host:443 →
    在 tunnel 上跑真实 TLS（ssl.MemoryBIO 模式）到 GitHub → 普通 HTTP/1.1 GET。

    这样弱网下不依赖宿主机直连 GitHub 的成功率，借用 VPS 出口带宽。

    含 cached_etag / cached_lm 时发条件请求；上游 304 → 不重下、返回 not_modified。

    返回：
      {"kind": "saved" | "not_modified", "etag": str|None, "last_modified": str|None}
    """
    extra: dict[str, str] = {}
    if cached_etag:
        extra["If-None-Match"] = cached_etag
    if cached_lm:
        extra["If-Modified-Since"] = cached_lm

    status, headers, body = await _https_get_via_pool(url, pool, extra_headers=extra or None)

    if status == 304:
        return {"kind": "not_modified",
                "etag": cached_etag, "last_modified": cached_lm}

    new_etag = next((v for k, v in headers if k.lower() == "etag"), None)
    new_lm   = next((v for k, v in headers if k.lower() == "last-modified"), None)
    tmp = dest + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        _check_dat_sane(tmp)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"kind": "saved", "etag": new_etag, "last_modified": new_lm}


async def _https_get_via_pool(
    url: str, pool,
    extra_headers: dict | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """
    返回 (status, response_headers, body)。
    调用方处理 200（含 body）/ 304（空 body）；其余非 3xx 抛 IOError。
    """
    for _ in range(_TUNNEL_MAX_REDIRECTS + 1):
        u = urllib.parse.urlparse(url)
        if u.scheme != "https":
            raise ValueError(f"only https supported, got {u.scheme!r}")
        host = u.hostname or ""
        port = u.port or 443
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")

        # 总超时：避免对端慢速喂数据让一个请求拖太久
        status, headers, body = await asyncio.wait_for(
            _https_request_once(host, port, path, pool, extra_headers),
            timeout=_TUNNEL_REQUEST_BUDGET,
        )

        # 304 不算 redirect，直接交给调用方
        if status == 304:
            return status, headers, body
        if 300 <= status < 400:
            loc = next((v for k, v in headers if k.lower() == "location"), None)
            if not loc:
                raise IOError(f"redirect {status} missing Location")
            # 用 urljoin 而非手拼，正确处理 RFC 3986 的所有相对形式：
            #   绝对 URL          https://x/y         → 用 loc
            #   scheme-relative   //x/y               → 用 https://x/y
            #   path-absolute     /y                  → 用 当前 host:port/y（保留端口）
            #   path-relative     y / ./y / ../y      → 按当前 path 解析
            url = urllib.parse.urljoin(url, loc.strip())
            logger.debug("geo redirect %d -> %s", status, url)
            continue
        if status != 200:
            raise IOError(f"HTTP {status}")
        return status, headers, body
    raise IOError(f"too many redirects ({_TUNNEL_MAX_REDIRECTS})")


async def _https_request_once(
    host: str, port: int, path: str, pool,
    extra_headers: dict | None = None,
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
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, EOFError):
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
        extra_lines = ""
        if extra_headers:
            extra_lines = "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: mirage-proxy/1.0\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n"
            f"{extra_lines}"
            f"\r\n"
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
    """
    解 HTTP/1.1 chunked 编码。
    严格要求看到 size=0 终止符，否则视为截断，抛错；
    否则中途断开会无声写出残缺 .dat。
    """
    out = bytearray()
    pos = 0
    seen_terminator = False
    while pos < len(data):
        nl = data.find(b"\r\n", pos)
        if nl < 0:
            break
        try:
            size = int(data[pos:nl].split(b";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            seen_terminator = True
            break
        pos = nl + 2
        if pos + size > len(data):
            break                  # chunk 头声明的字节数读不全 → 截断
        out.extend(data[pos:pos + size])
        pos += size + 2            # 跳过 chunk 数据 + 尾部 CRLF
    if not seen_terminator:
        raise IOError(
            f"chunked body truncated: no 0-size terminator chunk "
            f"({len(out)} bytes decoded so far)"
        )
    return bytes(out)


async def _ensure_one(
    key: str,
    url,                             # str 或 list[str]：支持多镜像 fallback
    cache_dir: str,
    update_days: float,
    meta: dict,
    pool=None,                       # 给定则每个 URL 都先试隧道再走直连
    force: bool = False,
) -> bool:
    urls = [url] if isinstance(url, str) else [u for u in url if u]
    if not urls:
        return False

    dest             = _dat_path(cache_dir, key)
    exists           = os.path.exists(dest)
    downloaded_at    = meta["sources"].get(key, {}).get("downloaded_at", 0)
    age_days         = (time.time() - downloaded_at) / 86400 if downloaded_at else float("inf")

    # 决定是否要重新下载 + 给出清晰的"为什么"，方便用户排查
    if force:
        reason = "force update"
        needs_download = True
    elif not exists:
        reason = "local .dat missing"
        needs_download = True
    elif downloaded_at == 0:
        reason = "no downloaded_at timestamp in meta.json (first run or meta lost)"
        needs_download = True
    elif age_days > update_days:
        reason = f"age {age_days:.1f}d > update_days {update_days:.1f}d"
        needs_download = True
    else:
        # 命中缓存：INFO 级别打出，让用户能看到"今天没下载"的事实
        logger.info("geo[%s] up-to-date, skip download (age %.1fd < %.1fd, file=%s)",
                    key, age_days, update_days, dest)
        return True

    logger.info("Downloading geo[%s] from %d mirror(s) — %s",
                key, len(urls), reason)

    # 条件 GET 的 etag / last_modified：仅当本地 .dat 还在时才透传，
    # 否则强制无条件下载（防 304 命中但本地文件已被删的尴尬）
    existing = meta["sources"].get(key, {})
    if exists and not force:
        cached_etag = existing.get("etag")
        cached_lm   = existing.get("last_modified")
    else:
        cached_etag = None
        cached_lm   = None

    def _record(u: str, result: dict, via: str, i: int) -> None:
        meta["sources"][key] = {
            "url":           u,
            "downloaded_at": int(time.time()),
            "etag":          result.get("etag"),
            "last_modified": result.get("last_modified"),
        }
        if result["kind"] == "not_modified":
            logger.info("geo[%s] not modified (304) via %s, revalidated (mirror %d/%d): %s",
                        key, via, i, len(urls), u)
        else:
            logger.info("geo[%s] saved via %s (mirror %d/%d, %d KB): %s",
                        key, via, i, len(urls), os.path.getsize(dest) // 1024, u)

    for i, u in enumerate(urls, start=1):
        # 优先：经我们自己的加密隧道（弱网更稳）
        if pool is not None:
            try:
                result = await _download_via_tunnel(u, dest, pool, cached_etag, cached_lm)
                _record(u, result, "tunnel", i)
                return True
            except Exception as e:
                logger.warning("geo[%s] tunnel mirror %d/%d failed: %s",
                               key, i, len(urls), e)

        # 兜底：直连
        try:
            result = await asyncio.to_thread(_download, u, dest, cached_etag, cached_lm)
            _record(u, result, "direct", i)
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
    force: bool = False,
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
            force       = force,
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

    ── 缓存命中策略 ──────────────────────────────────────────────────────────
    cfg 字段：
      geosite_dir         缓存目录（默认 ".geosite"）。**相对路径基于 CWD**——
                          建议写绝对路径，否则不同启动目录会找到不同的缓存
      geosite_update_days 默认刷新周期（天）。每个源可在 sources 里独立覆盖
      force_geosite_update 设为 true 时一律强制重新下载（默认 false）

    决定是否下载的判据（详见 _ensure_one 的日志输出）：
      1. force_geosite_update=true → 重下
      2. 本地 .dat 不存在 → 重下
      3. meta.json 缺该源的 downloaded_at → 重下
      4. 距上次下载 > update_days → 重下
      其余 → INFO 日志"skip download"，复用缓存
    """
    # **绝对路径化**：相对路径 CWD 漂移是"每次启动都下载"的常见根因
    cache_dir_raw = cfg.get("geosite_dir", ".geosite")
    cache_dir     = os.path.abspath(cache_dir_raw)
    if cache_dir != cache_dir_raw:
        logger.info("geo cache dir: %s (resolved from %r)", cache_dir, cache_dir_raw)
    else:
        logger.info("geo cache dir: %s", cache_dir)

    default_days = cfg.get("geosite_update_days", 7)
    force        = bool(cfg.get("force_geosite_update", False))

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

    # 透明诊断：从 meta.json 实际读到的内容
    n_entries = len(meta.get("sources", {}))
    meta_path = os.path.join(cache_dir, _META_FILE)
    if n_entries == 0:
        logger.info("meta.json empty / not found (%s) — all sources will download",
                    meta_path)
    else:
        logger.info("meta.json: %d source(s) tracked at %s", n_entries, meta_path)

    # geosite 和 geoip 并发下载
    site_task = _ensure_typed("site", site_sources, cache_dir, default_days,
                              meta, pool, force=force)
    ip_task   = _ensure_typed("ip",   ip_sources,   cache_dir, default_days,
                              meta, pool, force=force)
    site_paths, ip_paths = await asyncio.gather(site_task, ip_task)

    site_paths.update(site_fallback)
    _write_meta(cache_dir, meta)

    if site_paths:
        logger.info("geosite ready: %s", list(site_paths))
    if ip_paths:
        logger.info("geoip   ready: %s", list(ip_paths))

    return site_paths, ip_paths
