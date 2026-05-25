"""
分流路由

规则写在 config_client.json 的 "rules" 数组，格式与 Clash 兼容。

规则类型：
  DOMAIN,example.com,DIRECT
  DOMAIN-SUFFIX,google.com,PROXY
  DOMAIN-KEYWORD,youtube,PROXY
  IP-CIDR,192.168.0.0/16,DIRECT
  GEOSITE,[source:]tag,ACTION      geosite.dat 域名规则，可指定源
  GEOIP,[source:]code,ACTION       geoip.dat  IP 归属地规则，可指定源
  FINAL,PROXY                      默认动作，放最后

动作：
  PROXY   走加密隧道
  DIRECT  本地直连
  REJECT  拒绝连接

GEOSITE / GEOIP 引用示例：
  GEOSITE,loyalsoldier:category-ads-all,REJECT
  GEOSITE,cn,DIRECT                 ← 省略 source，使用第一个 geosite 源
  GEOIP,loyalsoldier:cn,DIRECT
  GEOIP,cn,DIRECT                   ← 省略 source，使用第一个 geoip 源
"""

from __future__ import annotations

import ipaddress
import re
import struct

from .bloom import BloomFilter
from .utils  import get_logger

logger = get_logger("router")

PROXY  = "PROXY"
DIRECT = "DIRECT"
REJECT = "REJECT"

_ACTIONS = {PROXY, DIRECT, REJECT}

# 从正则 pattern 提取最长固定字面量，用于跳过明显不匹配的主机名
# 策略：反转义 \X → X，去掉其余特殊字符，取最长含点的片段
_UNESCAPE = re.compile(r'\\(.)')
_NON_LIT  = re.compile(r'[^a-zA-Z0-9.-]')

def _extract_literal(pattern: str) -> str:
    s = _UNESCAPE.sub(lambda m: m.group(1), pattern)
    s = _NON_LIT.sub(' ', s)
    parts = [p for p in s.split() if '.' in p and len(p) >= 4]
    return max(parts, key=len) if parts else ''

# geosite Domain.Type
_GEO_KEYWORD = 0
_GEO_SUFFIX  = 2
_GEO_EXACT   = 3


# ── 最小化 Protobuf 解析器 ─────────────────────────────────────────────────────

def _varint(mv: memoryview, pos: int) -> tuple[int, int]:
    n = shift = 0
    while pos < len(mv):
        b = mv[pos]; pos += 1
        n |= (b & 0x7f) << shift
        if not (b & 0x80):
            return n, pos
        shift += 7
    raise ValueError("Truncated varint")


def _len_delim(mv: memoryview, pos: int) -> tuple[memoryview, int]:
    length, pos = _varint(mv, pos)
    return mv[pos: pos + length], pos + length


# ── geosite.dat 解析 ───────────────────────────────────────────────────────────

def _parse_domain_msg(mv: memoryview) -> tuple[int, str] | None:
    pos = 0; dtype = 0; value = ""
    while pos < len(mv):
        tag, pos = _varint(mv, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, pos = _varint(mv, pos)
            if fn == 1: dtype = v
        elif wt == 2:
            content, pos = _len_delim(mv, pos)
            if fn == 2: value = bytes(content).decode("utf-8", errors="replace")
        else: break
    return (dtype, value) if value else None


def _parse_geosite_entry(mv: memoryview) -> tuple[str, list[tuple[int, str]]] | None:
    pos = 0; code = ""; domains: list[tuple[int, str]] = []
    while pos < len(mv):
        tag, pos = _varint(mv, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            content, pos = _len_delim(mv, pos)
            if fn == 1: code = bytes(content).decode("utf-8", errors="replace").upper()
            elif fn == 2:
                d = _parse_domain_msg(content)
                if d: domains.append(d)
        elif wt == 0: _, pos = _varint(mv, pos)
        else: break
    return (code, domains) if code else None


def load_geosite_dat(path: str) -> dict[str, list[tuple[int, str]]]:
    """解析 geosite.dat → {TAG: [(dtype, value), ...]}"""
    with open(path, "rb") as f:
        data = memoryview(f.read())
    result: dict[str, list[tuple[int, str]]] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _varint(data, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                content, pos = _len_delim(data, pos)
                if fn == 1:
                    entry = _parse_geosite_entry(content)
                    if entry:
                        code, domains = entry
                        result[code] = domains
            elif wt == 0: _, pos = _varint(data, pos)
            else: break
        except Exception: break
    logger.info("geosite.dat: %d tags  (%s)", len(result), path)
    return result


# ── geoip.dat 解析 ────────────────────────────────────────────────────────────
#
# 协议：GeoIPList { repeated GeoIP entry = 1; }
#       GeoIP { string country_code = 1; repeated CIDR cidr = 2; bool inverse_match = 3; }
#       CIDR  { bytes ip = 1; uint32 prefix = 2; }

def _parse_cidr_msg(mv: memoryview) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    pos = 0; ip_bytes = b""; prefix = 0
    while pos < len(mv):
        tag, pos = _varint(mv, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            content, pos = _len_delim(mv, pos)
            if fn == 1: ip_bytes = bytes(content)
        elif wt == 0:
            v, pos = _varint(mv, pos)
            if fn == 2: prefix = v
        else: break
    if not ip_bytes:
        return None
    try:
        if len(ip_bytes) == 4:
            net_int = int.from_bytes(ip_bytes, "big")
            return ipaddress.IPv4Network((net_int, prefix), strict=False)
        elif len(ip_bytes) == 16:
            net_int = int.from_bytes(ip_bytes, "big")
            return ipaddress.IPv6Network((net_int, prefix), strict=False)
    except Exception:
        pass
    return None


def _parse_geoip_entry(mv: memoryview) -> tuple[str, list, bool] | None:
    """返回 (country_code, [networks], inverse_match)"""
    pos = 0; code = ""; networks = []; inverse = False
    while pos < len(mv):
        tag, pos = _varint(mv, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            content, pos = _len_delim(mv, pos)
            if fn == 1: code = bytes(content).decode("utf-8", errors="replace").upper()
            elif fn == 2:
                net = _parse_cidr_msg(content)
                if net: networks.append(net)
        elif wt == 0:
            v, pos = _varint(mv, pos)
            if fn == 3: inverse = bool(v)
        else: break
    return (code, networks, inverse) if code else None


def load_geoip_dat(path: str) -> dict[str, tuple[list, bool]]:
    """
    解析 geoip.dat → {COUNTRY_CODE: ([networks], inverse_match)}
    inverse_match=True 表示匹配"不属于此国"的 IP（如 !cn）
    """
    with open(path, "rb") as f:
        data = memoryview(f.read())
    result: dict[str, tuple[list, bool]] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _varint(data, pos)
            fn, wt = tag >> 3, tag & 7
            if wt == 2:
                content, pos = _len_delim(data, pos)
                if fn == 1:
                    entry = _parse_geoip_entry(content)
                    if entry:
                        code, nets, inv = entry
                        result[code] = (nets, inv)
            elif wt == 0: _, pos = _varint(data, pos)
            else: break
        except Exception: break
    logger.info("geoip.dat: %d regions  (%s)", len(result), path)
    return result


# ── GeoIP 快速查找 ─────────────────────────────────────────────────────────────

class _GeoIPTable:
    """
    将多个 geoip 源的规则合并成有序查找表。
    IPv4 按网络地址排序后二分定位候选段，再逐一验证包含关系；
    IPv6 数量通常较少，直接线性扫描。
    """

    def __init__(self) -> None:
        # [(network_int, prefix_len, broadcast_int, action), ...]  sorted by network_int
        self._v4: list[tuple[int, int, int, str]] = []
        self._v6: list[tuple[ipaddress.IPv6Network, str]] = []
        self._built = False

    def add_networks(
        self,
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
        action: str,
        inverse: bool = False,
    ) -> None:
        assert not self._built, "Cannot add after build()"
        for net in networks:
            if isinstance(net, ipaddress.IPv4Network):
                ni = int(net.network_address)
                bi = int(net.broadcast_address)
                if inverse:
                    # inverse match 少见，简化为直接存储并在 match() 时处理
                    self._v4.append((ni, net.prefixlen, bi, f"!{action}"))
                else:
                    self._v4.append((ni, net.prefixlen, bi, action))
            else:
                if inverse:
                    self._v6.append((net, f"!{action}"))
                else:
                    self._v6.append((net, action))

    def build(self) -> None:
        self._v4.sort(key=lambda x: (x[0], x[1]))
        self._built = True

    def match(self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        if isinstance(addr, ipaddress.IPv4Address):
            return self._match_v4(int(addr))
        return self._match_v6(addr)

    def _match_v4(self, addr_int: int) -> str | None:
        # 二分找到最后一个 network_int <= addr_int 的候选，再验证
        lo, hi = 0, len(self._v4) - 1
        best = -1
        while lo <= hi:
            mid = (lo + hi) >> 1
            if self._v4[mid][0] <= addr_int:
                best = mid; lo = mid + 1
            else:
                hi = mid - 1
        # 向前扫描所有可能覆盖 addr_int 的 CIDR（可能有多个重叠）
        i = best
        while i >= 0 and self._v4[i][0] <= addr_int:
            ni, _, bi, action = self._v4[i]
            if addr_int <= bi:
                return action.lstrip("!") if action.startswith("!") else action
            i -= 1
        return None

    def _match_v6(self, addr: ipaddress.IPv6Address) -> str | None:
        for net, action in self._v6:
            if addr in net:
                return action.lstrip("!") if action.startswith("!") else action
        return None


# ── 路由器 ─────────────────────────────────────────────────────────────────────

class Router:
    def __init__(self, default: str = PROXY):
        self._default  = default
        self._exact:   dict[str, str] = {}
        self._suffix:  dict[str, str] = {}
        self._keyword: list[tuple[str, str]] = []
        # 有可提取字面量的正则：[(literal, [(pattern, action), ...]), ...]
        # match() 先用 `literal in h`（C 层 BM 算法）预筛，命中才进正则引擎
        self._regex_groups:   list[tuple[str, list[tuple[re.Pattern, str]]]] = []
        # 无法提取字面量的正则，始终跑引擎
        self._regex_fallback: list[tuple[re.Pattern, str]] = []
        self._cidr:    list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]] = []
        self._geoip_table = _GeoIPTable()
        self._suffix_bf: BloomFilter | None = None

    def add_exact(self, domain: str, action: str) -> None:
        self._exact[domain.lower()] = action.upper()

    def add_suffix(self, domain: str, action: str) -> None:
        self._suffix[domain.lower()] = action.upper()

    def add_keyword(self, keyword: str, action: str) -> None:
        self._keyword.append((keyword.lower(), action.upper()))

    def add_regex(self, pattern: str, action: str) -> None:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning("Invalid regex pattern '%s': %s", pattern, e)
            return
        lit = _extract_literal(pattern)
        if lit:
            for g_lit, g_list in self._regex_groups:
                if g_lit == lit:
                    g_list.append((compiled, action.upper()))
                    return
            self._regex_groups.append((lit, [(compiled, action.upper())]))
        else:
            self._regex_fallback.append((compiled, action.upper()))

    def add_cidr(self, cidr: str, action: str) -> None:
        try:
            self._cidr.append((ipaddress.ip_network(cidr, strict=False), action.upper()))
        except ValueError:
            logger.warning("Invalid CIDR: %s", cidr)

    def add_geoip(
        self,
        networks: list,
        action: str,
        inverse: bool = False,
    ) -> None:
        self._geoip_table.add_networks(networks, action.upper(), inverse)

    def set_default(self, action: str) -> None:
        self._default = action.upper()

    def build(self) -> None:
        n = max(len(self._suffix), 1)
        self._suffix_bf = BloomFilter(capacity=n)
        for domain in self._suffix:
            self._suffix_bf.add(domain)
        self._geoip_table.build()
        n_regex = (sum(len(g[1]) for g in self._regex_groups)
                   + len(self._regex_fallback))
        logger.info(
            "Router built: %d exact / %d suffix / %d keyword / %d regex / %d cidr | default=%s",
            len(self._exact), len(self._suffix),
            len(self._keyword), n_regex, len(self._cidr),
            self._default,
        )

    def match(self, host: str) -> str:
        h = host.lower().rstrip(".")

        # IP 地址：先查静态 CIDR，再查 GeoIP 表
        try:
            addr = ipaddress.ip_address(h)
            for net, action in self._cidr:
                if addr in net:
                    return action
            geo_action = self._geoip_table.match(addr)
            if geo_action:
                return geo_action
            return self._default
        except ValueError:
            pass

        # 精确匹配
        if h in self._exact:
            return self._exact[h]

        # 后缀匹配（BF 预过滤）
        parts = h.split(".")
        for i in range(len(parts) - 1):
            suffix = ".".join(parts[i:])
            if self._suffix_bf and suffix not in self._suffix_bf:
                continue
            action = self._suffix.get(suffix)
            if action:
                return action

        # 关键词（线性）
        for kw, action in self._keyword:
            if kw in h:
                return action

        # 正则：字面量预筛（C 层 `in`）→ 命中才进正则引擎
        for lit, patterns in self._regex_groups:
            if lit in h:
                for pat, action in patterns:
                    if pat.search(h):
                        return action
        for pat, action in self._regex_fallback:
            if pat.search(h):
                return action

        return self._default


# ── 工厂函数 ───────────────────────────────────────────────────────────────────

def build_router(
    cfg: dict,
    available_site: dict[str, str],   # {source_name: geosite_dat_path}
    available_ip:   dict[str, str],   # {source_name: geoip_dat_path}
) -> Router:
    router = Router()

    # 懒加载缓存：同一文件只解析一次
    _site_cache: dict[str, dict[str, list[tuple[int, str]]]] = {}
    _ip_cache:   dict[str, dict[str, tuple[list, bool]]]     = {}

    def _get_site(name: str):
        if name not in _site_cache:
            path = available_site.get(name)
            if not path:
                logger.warning("geosite source '%s' not available", name)
                _site_cache[name] = {}
            else:
                try:    _site_cache[name] = load_geosite_dat(path)
                except OSError as e:
                    logger.warning("Cannot read geosite '%s': %s", path, e)
                    _site_cache[name] = {}
        return _site_cache[name]

    def _get_ip(name: str):
        if name not in _ip_cache:
            path = available_ip.get(name)
            if not path:
                logger.warning("geoip source '%s' not available", name)
                _ip_cache[name] = {}
            else:
                try:    _ip_cache[name] = load_geoip_dat(path)
                except OSError as e:
                    logger.warning("Cannot read geoip '%s': %s", path, e)
                    _ip_cache[name] = {}
        return _ip_cache[name]

    default_site = next(iter(available_site), None)
    default_ip   = next(iter(available_ip),   None)

    for raw in cfg.get("rules", []):
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",", 2)]
        rule_type = parts[0].upper()

        if rule_type == "FINAL":
            if len(parts) >= 2:
                router.set_default(parts[1])
            continue

        if len(parts) < 3:
            continue

        value  = parts[1]
        action = parts[2].upper()

        if action not in _ACTIONS:
            logger.warning("Unknown action '%s', skipping: %s", action, line)
            continue

        if rule_type == "DOMAIN":
            router.add_exact(value, action)
            logger.info("Rule: DOMAIN          %-40s → %s", value, action)

        elif rule_type == "DOMAIN-SUFFIX":
            router.add_suffix(value, action)
            logger.info("Rule: DOMAIN-SUFFIX   %-40s → %s", value, action)

        elif rule_type == "DOMAIN-KEYWORD":
            router.add_keyword(value, action)
            logger.info("Rule: DOMAIN-KEYWORD  %-40s → %s", value, action)

        elif rule_type == "DOMAIN-REGEX":
            router.add_regex(value, action)
            logger.info("Rule: DOMAIN-REGEX    %-40s → %s", value, action)

        elif rule_type == "IP-CIDR":
            router.add_cidr(value, action)
            logger.info("Rule: IP-CIDR         %-40s → %s", value, action)

        elif rule_type == "GEOSITE":
            src, tag = (value.split(":", 1) if ":" in value
                        else (default_site or "", value))
            if not src:
                logger.warning("GEOSITE '%s': no source, skipped", value)
                continue
            _apply_geosite(router, _get_site(src), tag.upper(), action, src)

        elif rule_type == "GEOIP":
            src, code = (value.split(":", 1) if ":" in value
                         else (default_ip or "", value))
            if not src:
                logger.warning("GEOIP '%s': no source, skipped", value)
                continue
            _apply_geoip(router, _get_ip(src), code.upper(), action, src)

    router.build()
    return router


def _apply_geosite(router, geosite, tag, action, source=""):
    entries = geosite.get(tag)
    if entries is None:
        logger.warning("GEOSITE tag '%s' not in '%s'", tag, source)
        return
    n_exact = n_suffix = n_kw = n_skip = 0
    for dtype, value in entries:
        if dtype == _GEO_EXACT:
            router.add_exact(value, action);   n_exact  += 1
        elif dtype == _GEO_SUFFIX:
            router.add_suffix(value, action);  n_suffix += 1
        elif dtype == _GEO_KEYWORD:
            router.add_keyword(value, action); n_kw     += 1
        else:
            n_skip += 1
    logger.info("GEOSITE[%s]:%s → %s  exact=%d suffix=%d kw=%d skip=%d",
                source, tag, action, n_exact, n_suffix, n_kw, n_skip)


def _apply_geoip(router, geoip, code, action, source=""):
    entry = geoip.get(code)
    if entry is None:
        logger.warning("GEOIP code '%s' not in '%s'", code, source)
        return
    networks, inverse = entry
    router.add_geoip(networks, action, inverse)
    logger.info("GEOIP[%s]:%s → %s  networks=%d inverse=%s",
                source, code, action, len(networks), inverse)
