"""
分流路由（sing-box 风格结构化 rules + 老 CSV 兼容）

═══ 新格式（推荐，sing-box 子集） ═══

  config_client.json:
    "route": {
        "rules": [
            {"rule_set": ["loyalsoldier:netflix"], "outbound": "us-fallback"},
            {"domain_suffix": [".openai.com"],     "outbound": "us-fallback"},
            {"rule_set": ["loyalsoldier:gfw"],     "outbound": "auto"},
            {"ip_cidr": ["192.168.0.0/16"],        "outbound": "direct"},
            {"geoip": ["loyalsoldier:cn"],         "outbound": "direct"}
        ],
        "final": "direct"
    }

  字段（每条 rule 只允许一种 criterion 字段；多字段视为非法 — sing-box
  里多字段是 AND 语义，本实现不支持，避免误解）：
    domain          精确匹配
    domain_suffix   后缀
    domain_keyword  子串
    domain_regex    正则
    ip_cidr         CIDR
    rule_set        geosite tag（带 [source:] 前缀，省略则取首个 geosite 源）
    geoip           geoip code（同上）
    invert          true 时整条规则取反（典型用法配 geoip 实现 "!cn"）
    outbound        命中后路由到的 outbound tag（必填）

═══ 老格式（向后兼容） ═══

  顶层 "rules" 是字符串数组（CSV）。逐行从上到下扫描：
    DOMAIN,example.com,DIRECT
    DOMAIN-SUFFIX,google.com,PROXY
    DOMAIN-KEYWORD,youtube,PROXY
    DOMAIN-REGEX,^(.+\\.)?google\\.com$,PROXY
    IP-CIDR,192.168.0.0/16,DIRECT
    GEOSITE,[source:]tag,ACTION
    GEOIP,[source:]code,ACTION
    FINAL,PROXY

  老 action 关键字 PROXY / DIRECT / REJECT 会被映射到 outbound tag：
    PROXY  → cfg 中合成的 pyrealiy outbound（默认 tag "proxy"）
    DIRECT → 自动补全的 "direct" outbound
    REJECT → 自动补全的 "block" outbound

═══ 实现要点 ═══

  每条规则被解析为一个独立的 _Rule 对象，自带类型相关的优化匹配器
  （DOMAIN/exact 用 == ；SUFFIX 用 endswith；GEOSITE 内部自带 Bloom
  Filter + 三套字典；GEOIP 内部按 v4/v6 拆分并对 v4 做排序+二分；
  DOMAIN-REGEX 用固定字面量预筛）。Router.match() 线性扫描列表，
  首条返回 True 即给出 action。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Union

from .bloom import BloomFilter
from .utils import get_logger

logger = get_logger("router")

# 老 CSV 用的固定关键字（兼容模式下仍生效）
PROXY  = "PROXY"
DIRECT = "DIRECT"
REJECT = "REJECT"

# 从正则 pattern 提取最长固定字面量，用于跳过明显不匹配的主机名
#
# ── 正确性不变量 ─────────────────────────────────────────────────────────────
# 返回的 literal L 必须是 pat.search(s) 能匹配的任意字符串 s 的子串。
# 否则 `L not in s` 的快速 prefilter 会把合法匹配错误地拒掉，路由规则失效。
#
# ── 老实现的两个 bug（已修复） ──────────────────────────────────────────────
# 老实现 = 去转义 + 替换非字面字符为空格 + split 取最长，存在两个不安全路径：
#
#  1. Alternation：`^(google\.com|youtube\.com)$` → 老代码先 unescape 成
#     `^(google.com|youtube.com)$`，再把 `^()|$` 替换成空格，split 得到两个
#     候选 "google.com" / "youtube.com"，取较长的 "youtube.com"。
#     prefilter 检查 `"youtube.com" not in "google.com"` → True → 错误返回
#     未匹配。任何含 `|` 的正则都几乎必然命中此 bug。
#
#  2. 转义元字符：`google\.com\d+` → unescape 把 `\.` 变 `.`、把 `\d` 变 `d`，
#     得到 "google.comd"。prefilter 检查 `"google.comd" not in "google.com123"`
#     → True → 错误返回未匹配。所有 `\d \s \w` 都有此问题。
#
# ── 新实现 ────────────────────────────────────────────────────────────────────
# 用 stdlib 的 regex parser 把 pattern 解析成 op 序列，只在**顶层**遍历，
# 把连续的 LITERAL op 聚成一段；任何非 LITERAL op（BRANCH / IN / 重复 /
# SUBPATTERN / 锚点等）打断当前 run。最终取最长 run。
#
# 不递归子组：`(google\.com)` 只在顶层产生一个 SUBPATTERN op，run 立即被打断，
# literal 留空 → prefilter 跳过 → 正则引擎全跑。这是"宁可少加速、绝不错拦"。
# 后续若想覆盖更多场景可以递归到单分支子组，但本次只追求**正确**的最小修复。
try:
    # Python 3.11+
    from re._parser import LITERAL as _RE_LITERAL, parse as _re_parse
except (ImportError, AttributeError):
    # Python 3.9 / 3.10
    from sre_parse import LITERAL as _RE_LITERAL, parse as _re_parse


def _extract_literal(pattern: str) -> str:
    try:
        parsed = _re_parse(pattern)
    except Exception:
        return ""

    best = ""
    cur: list[str] = []
    for op, val in parsed:
        if op == _RE_LITERAL:
            cur.append(chr(val))
            continue
        # 任何非 LITERAL op 打断 run
        if len(cur) > len(best):
            best = "".join(cur)
        cur = []
    if len(cur) > len(best):
        best = "".join(cur)

    # 正则用 re.IGNORECASE 编译，host 在 Router.match() 已 .lower()，
    # 故 literal 也要小写化才能作 substring 比较
    best = best.lower()
    # 沿用老过滤：太短或不含 '.' 的字面量不值得做 prefilter
    if "." in best and len(best) >= 4:
        return best
    return ""

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


# Clash 风格规则 type 映射（Yacd UI 用）
_CLASH_RULE_TYPE = {
    "DOMAIN":          "Domain",
    "DOMAIN-SUFFIX":   "DomainSuffix",
    "DOMAIN-KEYWORD":  "DomainKeyword",
    "DOMAIN-REGEX":    "DomainRegex",
    "IP-CIDR":         "IPCIDR",
    "IPCIDR":          "IPCIDR",
    "GEOIP":           "GeoIP",
    "GEOSITE":         "GeoSite",
    "FINAL":           "Match",
    "PORT":            "DstPort",
    "SRC-PORT":        "SrcPort",
    "PROCESS-NAME":    "ProcessName",
}


def _clash_rule_type(raw: str) -> str:
    return _CLASH_RULE_TYPE.get(raw.upper(), raw)


class _Rule:
    """
    单条规则基类。

    matches() 返回值有三态：
      False / None    未命中
      True            命中（无额外细节，沿用 self.desc 作日志）
      str             命中（附带子匹配细节，如 GEOSITE 内部哪条 exact/suffix/keyword）

    日志归因：dispatch 日志中显示 [rule.desc (detail)]，detail 为空时省略。

    invert: True 时整条规则命中/未命中取反（典型场景：GEOIP 实现 "非 CN"）。
            invert + 带 detail 的命中 → 视为未命中；invert + 未命中 → 命中无 detail。

    action 保持原样不做 upper()——sing-box 风格的 outbound tag 是用户自定义
    的字符串（含 case），改大小写会破坏匹配；CSV 路径在解析时已经统一大写。
    """
    __slots__ = ("action", "desc", "invert")
    def __init__(self, action: str, desc: str = "", invert: bool = False):
        self.action = action
        self.desc   = desc
        self.invert = invert
    def matches(self, host: str, addr: _AddrType):
        raw = self._inner(host, addr)
        if self.invert:
            return False if raw else True
        return raw
    def _inner(self, host: str, addr: _AddrType):
        raise NotImplementedError


class _ExactRule(_Rule):
    __slots__ = ("_d",)
    def __init__(self, domain: str, action: str, invert: bool = False):
        super().__init__(action, f"DOMAIN {domain}", invert)
        self._d = domain.lower()
    def _inner(self, host, _addr):
        return host == self._d


class _SuffixRule(_Rule):
    __slots__ = ("_d", "_dot")
    def __init__(self, domain: str, action: str, invert: bool = False):
        super().__init__(action, f"DOMAIN-SUFFIX {domain}", invert)
        # 容忍用户写 ".netflix.com" 或 "netflix.com" 两种形式
        # （sing-box / Clash 习惯不带前导点；我们多剥一下不破坏后缀语义）
        self._d   = domain.lower().lstrip(".")
        self._dot = "." + self._d
    def _inner(self, host, _addr):
        return host == self._d or host.endswith(self._dot)


class _KeywordRule(_Rule):
    __slots__ = ("_k",)
    def __init__(self, keyword: str, action: str, invert: bool = False):
        super().__init__(action, f"DOMAIN-KEYWORD {keyword}", invert)
        self._k = keyword.lower()
    def _inner(self, host, _addr):
        return self._k in host


class _RegexRule(_Rule):
    """带固定字面量预筛的正则规则；字面量为空时直接跑正则引擎"""
    __slots__ = ("_lit", "_pat")
    def __init__(self, pattern: str, action: str, compiled: re.Pattern, invert: bool = False):
        super().__init__(action, f"DOMAIN-REGEX {pattern}", invert)
        self._lit = _extract_literal(pattern)
        self._pat = compiled
    def _inner(self, host, _addr):
        if self._lit and self._lit not in host:
            return False
        return self._pat.search(host) is not None


class _CidrRule(_Rule):
    __slots__ = ("_net",)
    def __init__(self, cidr: str, action: str, net, invert: bool = False):
        super().__init__(action, f"IP-CIDR {cidr}", invert)
        self._net = net
    def _inner(self, _host, addr):
        return addr is not None and addr in self._net


class _GeositeRule(_Rule):
    """
    一条 GEOSITE 规则展开后内部维护 exact/suffix/keyword 三套表，
    suffix 量超过阈值时启用 Bloom Filter 预筛，与展开前同等查询性能。
    """
    __slots__ = ("_exact", "_suffix", "_keywords", "_bloom")
    _BLOOM_THRESHOLD = 64

    def __init__(self, tag: str, action: str, entries: list[tuple[int, str]], invert: bool = False):
        super().__init__(action, f"GEOSITE {tag}", invert)
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

    def _inner(self, host, _addr):
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

    def __init__(self, code: str, action: str, networks: list, inverse: bool, invert: bool = False):
        super().__init__(action, f"GEOIP {'!' if inverse else ''}{code}", invert)
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
        # 数据层的 inverse（来自 .dat 文件本身，如 !cn 条目）
        # 用户层 invert（来自 rule 定义的 "invert": true）由基类的 wrapper 处理
        self._inverse = inverse

    def _inner(self, _host, addr):
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

    action 字段是 outbound tag（结构化模式下保留 case）或老的 PROXY/DIRECT/
    REJECT 关键字（CSV 模式下已统一大写）。Router 本身不区分两种语义 ——
    上层 dispatch 时按字符串查 outbound 字典即可。
    """

    def __init__(self, default: str = PROXY):
        self._default = default
        self._rules: list[_Rule] = []
        # 独立计数器：以后若引入 remove/replace，索引仍单调递增不会乱序
        self._next_idx = 1

    def set_default(self, action: str) -> None:
        self._default = action

    @property
    def default(self) -> str:
        """当前 FINAL action（外部只读访问；避免调用方碰 _default 私有名）"""
        return self._default

    @property
    def rules(self) -> list[dict]:
        """
        给 Clash API（/rules）用的只读视图。每条规则一个 dict:
          {"type": "DomainSuffix", "payload": "google.com", "proxy": "<tag>", "invert": False}

        type 字段从规则 desc 的 "#N KIND value" 格式抽出 KIND 并映射成 Clash 风格
        CamelCase（DomainSuffix / Domain / IPCIDR / GeoIP 等）。Yacd 接受任意字符串。
        """
        out: list[dict] = []
        for r in self._rules:
            # desc 形如 "#3 DOMAIN-SUFFIX google.com"
            parts = r.desc.split(" ", 2)
            if len(parts) >= 3:
                kind_raw, payload = parts[1], parts[2]
            elif len(parts) == 2:
                kind_raw, payload = parts[1], ""
            else:
                kind_raw, payload = r.desc, ""
            out.append({
                "type":    _clash_rule_type(kind_raw),
                "payload": payload,
                "proxy":   r.action,
                "invert":  bool(r.invert),
            })
        return out

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
          action — 命中规则的 outbound tag（结构化模式下原样；CSV 模式下若 caller
                   提供了 legacy_action_map，关键字 PROXY/DIRECT/REJECT 已被映射）
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


# ── 数据源懒加载缓存 ──────────────────────────────────────────────────────────

class _GeoDataCache:
    """同一 geosite/geoip 文件只解析一次"""
    def __init__(self, available_site, available_ip):
        self.site_avail = available_site
        self.ip_avail   = available_ip
        self.default_site = next(iter(available_site), None)
        self.default_ip   = next(iter(available_ip),   None)
        self._site_cache: dict[str, dict[str, list[tuple[int, str]]]] = {}
        self._ip_cache:   dict[str, dict[str, tuple[list, bool]]]     = {}

    def site(self, name: str):
        if name not in self._site_cache:
            path = self.site_avail.get(name)
            if not path:
                logger.warning("geosite source '%s' not available", name)
                self._site_cache[name] = {}
            else:
                try:
                    self._site_cache[name] = load_geosite_dat(path)
                except OSError as e:
                    logger.warning("Cannot read geosite '%s': %s", path, e)
                    self._site_cache[name] = {}
        return self._site_cache[name]

    def ip(self, name: str):
        if name not in self._ip_cache:
            path = self.ip_avail.get(name)
            if not path:
                logger.warning("geoip source '%s' not available", name)
                self._ip_cache[name] = {}
            else:
                try:
                    self._ip_cache[name] = load_geoip_dat(path)
                except OSError as e:
                    logger.warning("Cannot read geoip '%s': %s", path, e)
                    self._ip_cache[name] = {}
        return self._ip_cache[name]


# ── 规则构造（内部辅助）──────────────────────────────────────────────────────

def _make_geosite_rule(value: str, action: str, cache: _GeoDataCache, invert: bool):
    src, tag = (value.split(":", 1) if ":" in value
                else (cache.default_site or "", value))
    if not src:
        logger.warning("GEOSITE '%s': no source, skipped", value)
        return None
    entries = cache.site(src).get(tag.upper())
    if entries is None:
        logger.warning("GEOSITE tag '%s' not in '%s'", tag, src)
        return None
    logger.info("Rule: GEOSITE         %-40s -> %s  (%d entries%s)",
                f"{src}:{tag}", action, len(entries), ", invert" if invert else "")
    return _GeositeRule(f"{src}:{tag}", action, entries, invert)


def _make_geoip_rule(value: str, action: str, cache: _GeoDataCache, invert: bool):
    src, code = (value.split(":", 1) if ":" in value
                 else (cache.default_ip or "", value))
    if not src:
        logger.warning("GEOIP '%s': no source, skipped", value)
        return None
    entry = cache.ip(src).get(code.upper())
    if entry is None:
        logger.warning("GEOIP code '%s' not in '%s'", code, src)
        return None
    networks, inverse = entry
    logger.info("Rule: GEOIP           %-40s -> %s  (%d networks, data-inverse=%s%s)",
                f"{src}:{code}", action, len(networks), inverse, ", invert" if invert else "")
    return _GeoipRule(f"{src}:{code}", action, networks, inverse, invert)


def _make_cidr_rule(value: str, action: str, invert: bool):
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        logger.warning("Invalid CIDR: %s", value)
        return None
    logger.info("Rule: IP-CIDR         %-40s -> %s%s", value, action, ", invert" if invert else "")
    return _CidrRule(value, action, net, invert)


def _make_regex_rule(value: str, action: str, invert: bool):
    try:
        compiled = re.compile(value, re.IGNORECASE)
    except re.error as e:
        logger.warning("Invalid regex pattern '%s': %s", value, e)
        return None
    logger.info("Rule: DOMAIN-REGEX    %-40s -> %s%s", value, action, ", invert" if invert else "")
    return _RegexRule(value, action, compiled, invert)


# ── 结构化（sing-box 风格）rules 解析 ─────────────────────────────────────────

# sing-box 字段名 → 内部 criterion 名
_STRUCT_FIELDS = {
    "domain":         "DOMAIN",
    "domain_suffix":  "DOMAIN-SUFFIX",
    "domain_keyword": "DOMAIN-KEYWORD",
    "domain_regex":   "DOMAIN-REGEX",
    "ip_cidr":        "IP-CIDR",
    "rule_set":       "GEOSITE",   # sing-box 用 rule_set 统称外部规则集
    "geoip":          "GEOIP",
}


def _apply_structured_rules(
    router: Router,
    rules: list,
    cache: _GeoDataCache,
    valid_actions: set[str] | None,
) -> None:
    for entry in rules:
        if not isinstance(entry, dict):
            logger.warning("rule entry must be object, skipped: %r", entry)
            continue
        action = entry.get("outbound")
        if not action or not isinstance(action, str):
            logger.warning("rule missing/invalid 'outbound', skipped: %r", entry)
            continue
        if valid_actions is not None and action not in valid_actions:
            logger.warning("rule 'outbound'='%s' not in defined outbounds, skipped: %r",
                           action, entry)
            continue
        invert = bool(entry.get("invert", False))

        # 找出本 rule 用的 criterion 字段（只允许一个）
        crit_fields = [k for k in _STRUCT_FIELDS if k in entry]
        if not crit_fields:
            logger.warning("rule has no criterion field, skipped: %r", entry)
            continue
        if len(crit_fields) > 1:
            logger.warning("rule has multiple criteria (only one supported), skipped: %r", entry)
            continue

        field = crit_fields[0]
        values = entry[field]
        if not isinstance(values, list):
            values = [values]

        # 数组语义 = OR：把每个 value 展开成一条独立 _Rule
        for v in values:
            v = str(v).strip()
            if not v:
                continue
            rule = _make_rule(field, v, action, cache, invert)
            if rule is not None:
                router.add(rule)


def _make_rule(field: str, value: str, action: str, cache: _GeoDataCache, invert: bool):
    if field == "domain":
        logger.info("Rule: DOMAIN          %-40s -> %s%s",
                    value, action, ", invert" if invert else "")
        return _ExactRule(value, action, invert)
    if field == "domain_suffix":
        logger.info("Rule: DOMAIN-SUFFIX   %-40s -> %s%s",
                    value, action, ", invert" if invert else "")
        return _SuffixRule(value, action, invert)
    if field == "domain_keyword":
        logger.info("Rule: DOMAIN-KEYWORD  %-40s -> %s%s",
                    value, action, ", invert" if invert else "")
        return _KeywordRule(value, action, invert)
    if field == "domain_regex":
        return _make_regex_rule(value, action, invert)
    if field == "ip_cidr":
        return _make_cidr_rule(value, action, invert)
    if field == "rule_set":
        return _make_geosite_rule(value, action, cache, invert)
    if field == "geoip":
        return _make_geoip_rule(value, action, cache, invert)
    return None


# ── CSV（老格式）rules 解析 ──────────────────────────────────────────────────

def _apply_csv_rules(
    router: Router,
    rules: list,
    cache: _GeoDataCache,
    legacy_action_map: dict[str, str] | None,
    valid_actions: set[str] | None,
) -> None:
    """
    老格式："DOMAIN-SUFFIX,xxx,PROXY" 风格的字符串行。

    legacy_action_map：把 CSV 里的关键字 PROXY/DIRECT/REJECT 映射到实际 outbound tag。
    None 时保持原 keyword（仅在 server 端 egress 路由这种自定义动作集场景使用）。
    """
    for raw in rules:
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 2)]
        rule_type = parts[0].upper()

        if rule_type == "FINAL":
            if len(parts) >= 2:
                raw    = parts[1]
                # 提供 legacy_action_map → client 端混用：新 tag 保留原 case
                # 不提供 → server 端 egress 语义：与老代码一致总是 upper
                #   （server egress 名约定大写 WARP/DIRECT 等，老 CSV 也是这样匹配的）
                if legacy_action_map is not None:
                    mapped = legacy_action_map.get(raw.upper(), raw)
                else:
                    mapped = raw.upper()
                router.set_default(mapped)
            continue

        if len(parts) < 3:
            continue

        value  = parts[1]
        raw    = parts[2]
        action = raw.upper()
        if legacy_action_map is not None:
            mapped = legacy_action_map.get(action, raw)
        else:
            mapped = action   # 老 server 端语义：CSV action 一律转大写

        if valid_actions is not None and mapped not in valid_actions:
            logger.warning("Unknown action '%s' (mapped to '%s'), skipping: %s",
                           action, mapped, line)
            continue

        rule = None
        if rule_type == "DOMAIN":
            rule = _ExactRule(value, mapped)
            logger.info("Rule: DOMAIN          %-40s -> %s", value, mapped)
        elif rule_type == "DOMAIN-SUFFIX":
            rule = _SuffixRule(value, mapped)
            logger.info("Rule: DOMAIN-SUFFIX   %-40s -> %s", value, mapped)
        elif rule_type == "DOMAIN-KEYWORD":
            rule = _KeywordRule(value, mapped)
            logger.info("Rule: DOMAIN-KEYWORD  %-40s -> %s", value, mapped)
        elif rule_type == "DOMAIN-REGEX":
            rule = _make_regex_rule(value, mapped, False)
        elif rule_type == "IP-CIDR":
            rule = _make_cidr_rule(value, mapped, False)
        elif rule_type == "GEOSITE":
            rule = _make_geosite_rule(value, mapped, cache, False)
        elif rule_type == "GEOIP":
            rule = _make_geoip_rule(value, mapped, cache, False)

        if rule is not None:
            router.add(rule)


# ── 工厂函数 ──────────────────────────────────────────────────────────────────

def build_router(
    cfg: dict,
    available_site: dict[str, str],     # {source_name: geosite_dat_path}
    available_ip:   dict[str, str],     # {source_name: geoip_dat_path}
    valid_actions: set[str] | None = None,    # 合法 action 集（client outbound tag / server egress 名）；None=不校验
    legacy_action_map: dict[str, str] | None = None,  # CSV 老 keyword → 实际 action
    rules_field:   str = "rules",       # server 端用 "egress_rules"
) -> Router:
    """
    自动判别 cfg 的 rules 格式：

      1. cfg["route"]["rules"] 存在 → 结构化模式
         （final 取 cfg["route"]["final"]）
      2. cfg["rules"] 是 list 且首元素是 dict → 结构化模式（顶层 rules）
         （final 取顶层 cfg["final"]）
      3. cfg["rules"] 是 list 且首元素是 str → CSV 模式
         （final 取 CSV 行 "FINAL,X"）

    未指定 final 时：
      legacy_action_map 提供则取其中的 PROXY → tag 映射，否则取 "direct"。

    server 端的 egress_rules 字段总是 CSV（无 route 嵌套），传 rules_field 指定。
    """
    # 初始默认动作。CSV 模式下老 PROXY 关键字会被映射；结构化模式下若未指定
    # final 字段则用 "direct" —— 与 sing-box 一致，避免未命中规则时悄悄走代理。
    initial_default = (legacy_action_map.get(PROXY, PROXY) if legacy_action_map else "direct")
    router = Router(default=initial_default)
    cache = _GeoDataCache(available_site, available_ip)

    # ── 决定走哪条路径 ────────────────────────────────────────────────────
    route_block = cfg.get("route") if rules_field == "rules" else None
    if isinstance(route_block, dict) and "rules" in route_block:
        rules_list = route_block.get("rules", []) or []
        final_action = route_block.get("final")
        if final_action:
            router.set_default(str(final_action))
        _apply_structured_rules(router, rules_list, cache, valid_actions)
    else:
        rules_list = cfg.get(rules_field, []) or []
        if rules_list and isinstance(rules_list[0], dict):
            # 顶层结构化 rules + 可选顶层 final
            final_action = cfg.get("final")
            if final_action:
                router.set_default(str(final_action))
            _apply_structured_rules(router, rules_list, cache, valid_actions)
        else:
            _apply_csv_rules(router, rules_list, cache, legacy_action_map, valid_actions)

    # final / FINAL 校验：避免 router._default 是未定义 outbound 时悄无声息地等运行期才报错
    if valid_actions is not None and router._default not in valid_actions:
        logger.warning("router default action '%s' is not a defined outbound; "
                       "未命中任何规则的请求会被 dispatch 端拒绝", router._default)

    router.build()
    return router
