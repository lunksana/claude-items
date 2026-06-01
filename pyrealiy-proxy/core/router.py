"""
分流路由（Clash 兼容语义）

规则写在 config_client.json 的 "rules" 数组，**按配置顺序从上到下扫描，首条命中即返回**，
后续规则不再执行。这与 Clash / sing-box / Surge 行为一致。

规则类型：
  DOMAIN,example.com,DIRECT
  DOMAIN-SUFFIX,google.com,PROXY
  DOMAIN-KEYWORD,youtube,PROXY
  DOMAIN-REGEX,^(.+\\.)?google\\.com$,PROXY
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

实现方式：
  每条规则被解析为一个独立的 _Rule 对象，自带类型相关的优化匹配器
  （DOMAIN/exact 用 == ；SUFFIX 用 endswith；GEOSITE 内部自带 Bloom Filter
  + 三套字典；GEOIP 内部按 v4/v6 拆分并对 v4 做排序+二分；DOMAIN-REGEX 用
  固定字面量预筛）。Router.match() 线性扫描列表，首条返回 True 即给出 action。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Union

from .bloom import BloomFilter
from .utils import get_logger

logger = get_logger("router")

PROXY  = "PROXY"
DIRECT = "DIRECT"
REJECT = "REJECT"
_ACTIONS = {PROXY, DIRECT, REJECT}

# 从正则 pattern 提取最长固定字面量，用于跳过明显不匹配的主机名
_UNESCAPE = re.compile(r"\\(.)")
_NON_LIT  = re.compile(r"[^a-zA-Z0-9.-]")

def _extract_literal(pattern: str) -> str:
    s = _UNESCAPE.sub(lambda m: m.group(1), pattern)
    s = _NON_LIT.sub(" ", s)
    parts = [p for p in s.split() if "." in p and len(p) >= 4]
    return max(parts, key=len) if parts else ""

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


# ── 单条规则匹配器 ─────────────────────────────────────────────────────────────

# 兼容 Python 3.9：X | Y | None 这种运行时联合类型语法是 PEP 604 引入的，要 3.10+
# 这里是赋值语句不是注解，from __future__ import annotations 也救不了 → 用 typing.Union
_AddrType = Union[ipaddress.IPv4Address, ipaddress.IPv6Address, None]


class _Rule:
    """
    单条规则基类。

    matches() 返回值有三态：
      False / None    未命中
      True            命中（无额外细节，沿用 self.desc 作日志）
      str             命中（附带子匹配细节，如 GEOSITE 内部哪条 exact/suffix/keyword）

    日志归因：dispatch 日志中显示 [rule.desc (detail)]，detail 为空时省略。
    """
    __slots__ = ("action", "desc")
    def __init__(self, action: str, desc: str = ""):
        self.action = action.upper()
        self.desc   = desc
    def matches(self, host: str, addr: _AddrType):
        raise NotImplementedError


class _ExactRule(_Rule):
    __slots__ = ("_d",)
    def __init__(self, domain: str, action: str):
        super().__init__(action, f"DOMAIN {domain}")
        self._d = domain.lower()
    def matches(self, host, _addr):
        return host == self._d


class _SuffixRule(_Rule):
    __slots__ = ("_d", "_dot")
    def __init__(self, domain: str, action: str):
        super().__init__(action, f"DOMAIN-SUFFIX {domain}")
        self._d   = domain.lower()
        self._dot = "." + self._d
    def matches(self, host, _addr):
        return host == self._d or host.endswith(self._dot)


class _KeywordRule(_Rule):
    __slots__ = ("_k",)
    def __init__(self, keyword: str, action: str):
        super().__init__(action, f"DOMAIN-KEYWORD {keyword}")
        self._k = keyword.lower()
    def matches(self, host, _addr):
        return self._k in host


class _RegexRule(_Rule):
    """带固定字面量预筛的正则规则；字面量为空时直接跑正则引擎"""
    __slots__ = ("_lit", "_pat")
    def __init__(self, pattern: str, action: str, compiled: re.Pattern):
        super().__init__(action, f"DOMAIN-REGEX {pattern}")
        self._lit = _extract_literal(pattern)
        self._pat = compiled
    def matches(self, host, _addr):
        if self._lit and self._lit not in host:
            return False
        return self._pat.search(host) is not None


class _CidrRule(_Rule):
    __slots__ = ("_net",)
    def __init__(self, cidr: str, action: str, net):
        super().__init__(action, f"IP-CIDR {cidr}")
        self._net = net
    def matches(self, _host, addr):
        return addr is not None and addr in self._net


class _GeositeRule(_Rule):
    """
    一条 GEOSITE 规则展开后内部维护 exact/suffix/keyword 三套表，
    suffix 量超过阈值时启用 Bloom Filter 预筛，与展开前同等查询性能。
    """
    __slots__ = ("_exact", "_suffix", "_keywords", "_bloom")
    _BLOOM_THRESHOLD = 64

    def __init__(self, tag: str, action: str, entries: list[tuple[int, str]]):
        super().__init__(action, f"GEOSITE {tag}")
        self._exact:    set[str]  = set()
        self._suffix:   set[str]  = set()
        self._keywords: list[str] = []
        for dtype, value in entries:
            v = value.lower()
            if   dtype == _GEO_EXACT:   self._exact.add(v)
            elif dtype == _GEO_SUFFIX:  self._suffix.add(v)
            elif dtype == _GEO_KEYWORD: self._keywords.append(v)
        if len(self._suffix) >= self._BLOOM_THRESHOLD:
            self._bloom = BloomFilter(capacity=len(self._suffix))
            for s in self._suffix:
                self._bloom.add(s)
        else:
            self._bloom = None

    def matches(self, host, _addr):
        if host in self._exact:
            return f"exact {host}"
        if self._suffix:
            parts = host.split(".")
            for i in range(len(parts) - 1):
                sfx = ".".join(parts[i:])
                if self._bloom is not None and sfx not in self._bloom:
                    continue
                if sfx in self._suffix:
                    return f"suffix {sfx}"
        for kw in self._keywords:
            if kw in host:
                return f"keyword {kw}"
        return False


class _GeoipRule(_Rule):
    """
    GEOIP 规则：v4 排序 + 二分；v6 线性。
    inverse=True 时语义反转——IP 不在任何网段视为命中。
    """
    __slots__ = ("_v4", "_v6", "_inverse")

    def __init__(self, code: str, action: str, networks: list, inverse: bool):
        super().__init__(action, f"GEOIP {'!' if inverse else ''}{code}")
        v4: list[tuple[int, int]] = []
        v6: list[ipaddress.IPv6Network] = []
        for net in networks:
            if isinstance(net, ipaddress.IPv4Network):
                v4.append((int(net.network_address), int(net.broadcast_address)))
            else:
                v6.append(net)
        v4.sort()
        self._v4 = v4
        self._v6 = v6
        self._inverse = inverse

    def matches(self, _host, addr):
        if addr is None:
            return False
        if isinstance(addr, ipaddress.IPv4Address):
            inside = self._v4_contains(int(addr))
        else:
            inside = any(addr in n for n in self._v6)
        return inside != self._inverse

    def _v4_contains(self, addr_int: int) -> bool:
        v4 = self._v4
        lo, hi = 0, len(v4) - 1
        best = -1
        while lo <= hi:
            mid = (lo + hi) >> 1
            if v4[mid][0] <= addr_int:
                best = mid; lo = mid + 1
            else:
                hi = mid - 1
        i = best
        while i >= 0 and v4[i][0] <= addr_int:
            if v4[i][1] >= addr_int:
                return True
            i -= 1
        return False


# ── Router ────────────────────────────────────────────────────────────────────

class Router:
    """
    按配置顺序扫描 self._rules，首条 matches() 返回 True 即给出 action。
    未命中任何规则时返回 FINAL 指定的默认动作。
    """

    def __init__(self, default: str = PROXY):
        self._default = default.upper()
        self._rules: list[_Rule] = []
        # 独立计数器：以后若引入 remove/replace，索引仍单调递增不会乱序
        self._next_idx = 1

    def set_default(self, action: str) -> None:
        self._default = action.upper()

    def add(self, rule: _Rule) -> None:
        # 给规则编号，方便日志归因到具体 config 行
        rule.desc = f"#{self._next_idx} {rule.desc}"
        self._next_idx += 1
        self._rules.append(rule)

    def build(self) -> None:
        """统计日志（无后处理：各规则自带优化结构）"""
        counts: dict[str, int] = {}
        for r in self._rules:
            cls = type(r).__name__
            counts[cls] = counts.get(cls, 0) + 1
        logger.info(
            "Router built: %d rules total | %s | default=%s",
            len(self._rules),
            ", ".join(f"{k}={v}" for k, v in counts.items()),
            self._default,
        )

    def match(self, host: str) -> tuple[str, str]:
        """
        返回 (action, source)：
          action — 命中的动作 PROXY/DIRECT/REJECT
          source — 命中的规则描述（用于日志归因），未命中任何规则时为 "FINAL"
                   规则带 #N 索引，便于对应到 config 中的具体行；GEOSITE 额外
                   附带 (exact X / suffix X / keyword X) 细化哪条子条目命中
        """
        h = host.lower().rstrip(".")
        addr: _AddrType
        try:
            addr = ipaddress.ip_address(h)
        except ValueError:
            addr = None
        for rule in self._rules:
            result = rule.matches(h, addr)
            if not result:
                continue
            if isinstance(result, str):
                return rule.action, f"{rule.desc} ({result})"
            return rule.action, rule.desc
        return self._default, "FINAL"


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
                try:
                    _site_cache[name] = load_geosite_dat(path)
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
                try:
                    _ip_cache[name] = load_geoip_dat(path)
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
            router.add(_ExactRule(value, action))
            logger.info("Rule: DOMAIN          %-40s -> %s", value, action)

        elif rule_type == "DOMAIN-SUFFIX":
            router.add(_SuffixRule(value, action))
            logger.info("Rule: DOMAIN-SUFFIX   %-40s -> %s", value, action)

        elif rule_type == "DOMAIN-KEYWORD":
            router.add(_KeywordRule(value, action))
            logger.info("Rule: DOMAIN-KEYWORD  %-40s -> %s", value, action)

        elif rule_type == "DOMAIN-REGEX":
            try:
                compiled = re.compile(value, re.IGNORECASE)
            except re.error as e:
                logger.warning("Invalid regex pattern '%s': %s", value, e)
                continue
            router.add(_RegexRule(value, action, compiled))
            logger.info("Rule: DOMAIN-REGEX    %-40s -> %s", value, action)

        elif rule_type == "IP-CIDR":
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                logger.warning("Invalid CIDR: %s", value)
                continue
            router.add(_CidrRule(value, action, net))
            logger.info("Rule: IP-CIDR         %-40s -> %s", value, action)

        elif rule_type == "GEOSITE":
            src, tag = (value.split(":", 1) if ":" in value
                        else (default_site or "", value))
            if not src:
                logger.warning("GEOSITE '%s': no source, skipped", value)
                continue
            entries = _get_site(src).get(tag.upper())
            if entries is None:
                logger.warning("GEOSITE tag '%s' not in '%s'", tag, src)
                continue
            router.add(_GeositeRule(f"{src}:{tag}", action, entries))
            logger.info("Rule: GEOSITE         %-40s -> %s  (%d entries)",
                        f"{src}:{tag}", action, len(entries))

        elif rule_type == "GEOIP":
            src, code = (value.split(":", 1) if ":" in value
                         else (default_ip or "", value))
            if not src:
                logger.warning("GEOIP '%s': no source, skipped", value)
                continue
            entry = _get_ip(src).get(code.upper())
            if entry is None:
                logger.warning("GEOIP code '%s' not in '%s'", code, src)
                continue
            networks, inverse = entry
            router.add(_GeoipRule(f"{src}:{code}", action, networks, inverse))
            logger.info("Rule: GEOIP           %-40s -> %s  (%d networks, inverse=%s)",
                        f"{src}:{code}", action, len(networks), inverse)

    router.build()
    return router
