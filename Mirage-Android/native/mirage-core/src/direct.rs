//! 国内外分流: 目标判定 (直连 vs 代理 vs 拦截)。
//!
//! 分流策略 (与主流代理 App 一致):
//! ```text
//! DNS 查询 → 国内域名 / GeoSite 直连 → 返回真实 IP (上游 DNS 查询) → 客户端连真实 IP
//!             国外域名 / GeoSite 代理 → 返回 fake-IP → 客户端连 fake-IP → 走隧道
//!             屏蔽规则 / GeoSite 拦截 → 返回 0.0.0.0 (Sinkhole) / 阻断
//! TCP/UDP 目标:
//!   fake-IP        → 代理 (隧道)
//!   裸 IP, 命中 CN / GeoIP 直连 → 直连 (protect socket 走真实网络)
//!   裸 IP, 命中 GeoIP 拦截      → 阻断
//!   裸 IP, 非 CN               → 代理
//! ```
//!
//! 数据来源: `direct_cn_ipv4.rs` (内置 CN 段) + `direct_cn_domains.rs` (内置白名单)
//! + 动态加载的 `geosite.dat` / `geoip.dat` 规则集。

use std::net::IpAddr;

mod cn_ipv4 { include!("direct_cn_ipv4.rs"); }
mod cn_domains { include!("direct_cn_domains.rs"); }

/// 判断 IP 是否属于中国段 (二分查找预排序范围, ~7700 段在 O(log N) ≈ 13 次比对完成)。
pub fn is_cn_ip(ip: IpAddr) -> bool {
    let ip_u32 = match ip {
        IpAddr::V4(v4) => u32::from(v4),
        IpAddr::V6(_) => return false,
    };
    static CN_RANGES: std::sync::LazyLock<Vec<(u32, u32)>> = std::sync::LazyLock::new(|| {
        let mut ranges: Vec<(u32, u32)> = cn_ipv4::CN_IPV4
            .iter()
            .map(|&(net, prefix)| {
                let mask = if prefix == 0 { 0 } else { !0u32 << (32 - prefix) };
                let start = net & mask;
                let end = start | !mask;
                (start, end)
            })
            .collect();
        ranges.sort_unstable_by_key(|&(start, _)| start);
        ranges
    });

    CN_RANGES.binary_search_by(|&(start, end)| {
        if ip_u32 < start {
            std::cmp::Ordering::Greater
        } else if ip_u32 > end {
            std::cmp::Ordering::Less
        } else {
            std::cmp::Ordering::Equal
        }
    }).is_ok()
}

/// 判断域名是否"国内" (命中白名单或国内后缀 → 直连)。零堆内存分配。
pub fn is_cn_domain(domain: &str) -> bool {
    let d = domain.trim_end_matches('.');
    for suffix in cn_domains::CN_DOMAINS {
        if d.eq_ignore_ascii_case(suffix)
            || (d.len() > suffix.len()
                && d.ends_with(suffix)
                && d.as_bytes()[d.len() - suffix.len() - 1] == b'.')
        {
            return true;
        }
    }
    // 国内顶级后缀
    for tld in ["cn", "com.cn", "net.cn", "org.cn", "edu.cn", "gov.cn", "mil.cn"] {
        if d.eq_ignore_ascii_case(tld)
            || (d.len() > tld.len()
                && d.ends_with(tld)
                && d.as_bytes()[d.len() - tld.len() - 1] == b'.')
        {
            return true;
        }
    }
    false
}

/// 规则动作
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuleAction {
    Direct,
    Proxy,
    Block,
}

impl RuleAction {
    pub fn as_str(&self) -> &'static str {
        match self {
            RuleAction::Direct => "direct",
            RuleAction::Proxy => "proxy",
            RuleAction::Block => "block",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "direct" => RuleAction::Direct,
            "block" | "reject" => RuleAction::Block,
            _ => RuleAction::Proxy,
        }
    }
}

/// 规则类型
#[derive(Debug, Clone)]
pub enum RuleKind {
    Suffix(String),
    Exact(String),
    Keyword(String),
    Regex(String, Option<regex::Regex>),
    GeoSite(String),
    GeoIp(String),
    Cidr(String),
}

impl RuleKind {
    pub fn kind_str(&self) -> &'static str {
        match self {
            RuleKind::Suffix(_) => "suffix",
            RuleKind::Exact(_) => "exact",
            RuleKind::Keyword(_) => "keyword",
            RuleKind::Regex(..) => "regex",
            RuleKind::GeoSite(_) => "geosite",
            RuleKind::GeoIp(_) => "geoip",
            RuleKind::Cidr(_) => "cidr",
        }
    }

    pub fn pattern_str(&self) -> &str {
        match self {
            RuleKind::Suffix(s)
            | RuleKind::Exact(s)
            | RuleKind::Keyword(s)
            | RuleKind::Regex(s, _)
            | RuleKind::GeoSite(s)
            | RuleKind::GeoIp(s)
            | RuleKind::Cidr(s) => s,
        }
    }
}

/// 用户自定义分流规则 (优先于内置 CN 规则)。
#[derive(Default)]
pub struct CustomRules {
    pub rules: Vec<(RuleKind, RuleAction)>,
}

fn rules_store() -> &'static std::sync::RwLock<CustomRules> {
    use std::sync::OnceLock;
    static R: OnceLock<std::sync::RwLock<CustomRules>> = OnceLock::new();
    R.get_or_init(|| std::sync::RwLock::new(CustomRules::default()))
}

/// 直连 IP 集合: DNS 分流把"直连域名"解析出的真实 IP 记到这里
fn direct_ips() -> &'static std::sync::Mutex<std::collections::HashSet<IpAddr>> {
    use std::sync::OnceLock;
    static S: OnceLock<std::sync::Mutex<std::collections::HashSet<IpAddr>>> = OnceLock::new();
    S.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}

static BLOCK_QUIC: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);

pub fn set_block_quic(block: bool) {
    BLOCK_QUIC.store(block, std::sync::atomic::Ordering::Relaxed);
}

pub fn is_block_quic() -> bool {
    BLOCK_QUIC.load(std::sync::atomic::Ordering::Relaxed)
}

pub fn mark_direct_ip(ip: IpAddr) {
    direct_ips().lock().unwrap_or_else(|e| e.into_inner()).insert(ip);
}

pub fn is_direct_ip(ip: IpAddr) -> bool {
    direct_ips().lock().unwrap_or_else(|e| e.into_inner()).contains(&ip)
}

pub fn builtin_domains() -> Vec<String> {
    cn_domains::CN_DOMAINS.iter().map(|s| s.to_string()).collect()
}

pub fn builtin_ip_count() -> usize {
    cn_ipv4::CN_IPV4.len()
}

/// 规则命中统计: key = "kind|pattern|action", 值 = 命中次数 (原子)。
fn rule_hits() -> &'static std::sync::Mutex<std::collections::HashMap<String, std::sync::atomic::AtomicU64>> {
    use std::sync::OnceLock;
    static H: OnceLock<std::sync::Mutex<std::collections::HashMap<String, std::sync::atomic::AtomicU64>>> =
        OnceLock::new();
    H.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

fn record_rule_hit(kind: &str, pattern: &str, action: &str) {
    let key = format!("{kind}|{pattern}|{action}");
    let mut map = rule_hits().lock().unwrap_or_else(|e| e.into_inner());
    map.entry(key).or_default().fetch_add(1, std::sync::atomic::Ordering::Relaxed);
}

pub fn get_rule_hits() -> String {
    let map = rule_hits().lock().unwrap_or_else(|e| e.into_inner());
    let mut list: Vec<serde_json::Value> = Vec::new();
    for (key, hits) in map.iter() {
        let mut parts = key.splitn(3, '|');
        if let (Some(kind), Some(pattern), Some(action)) =
            (parts.next(), parts.next(), parts.next())
        {
            list.push(serde_json::json!({
                "kind": kind, "pattern": pattern, "action": action,
                "hits": hits.load(std::sync::atomic::Ordering::Relaxed),
            }));
        }
    }
    serde_json::to_string(&list).unwrap_or_else(|_| "[]".to_string())
}

pub fn reset_rule_hits() {
    rule_hits().lock().unwrap_or_else(|e| e.into_inner()).clear();
}

/// 设置自定义规则 (JNI 调用, App 启动/改规则时注入)。
pub fn set_custom_rules(json: &str) -> bool {
    let parsed: Result<serde_json::Value, _> = serde_json::from_str(json);
    let Ok(v) = parsed else { return false };
    let mut rules = CustomRules::default();

    if let Some(arr) = v.get("rules").and_then(|a| a.as_array()) {
        for item in arr {
            let (Some(kind), Some(pattern), Some(action_str)) =
                (item.get("kind").and_then(|x| x.as_str()),
                 item.get("pattern").and_then(|x| x.as_str()),
                 item.get("action").and_then(|x| x.as_str())) else { continue };
            let action = RuleAction::from_str(action_str);
            let pat_lower = pattern.trim().to_ascii_lowercase();

            let rule_kind = match kind.trim().to_ascii_lowercase().as_str() {
                "geosite" | "rule_set" => RuleKind::GeoSite(pattern.trim().to_string()),
                "geoip" => RuleKind::GeoIp(pattern.trim().to_string()),
                "cidr" | "ip-cidr" => RuleKind::Cidr(pattern.trim().to_string()),
                "exact" | "domain" => RuleKind::Exact(pat_lower),
                "keyword" | "domain-keyword" => RuleKind::Keyword(pat_lower),
                "regex" | "domain-regex" => {
                    let re = regex::Regex::new(pattern.trim()).ok();
                    RuleKind::Regex(pattern.trim().to_string(), re)
                }
                _ => RuleKind::Suffix(pat_lower),
            };
            rules.rules.push((rule_kind, action));
        }
    }
    *rules_store().write().unwrap_or_else(|e| e.into_inner()) = rules;
    true
}

fn domain_match(rule: &str, domain: &str) -> bool {
    let rule = rule.trim_end_matches('.');
    let d = domain.trim_end_matches('.');
    if d.eq_ignore_ascii_case(rule) {
        return true;
    }
    if d.len() > rule.len() && d.ends_with(rule) && d.as_bytes()[d.len() - rule.len() - 1] == b'.' {
        return true;
    }
    false
}

fn cidr_match(rule: &str, ip: IpAddr) -> bool {
    let Some((net, prefix)) = rule.split_once('/') else {
        return rule.parse::<IpAddr>().map(|r| r == ip).unwrap_or(false);
    };
    let Ok(net) = net.parse::<IpAddr>() else { return false };
    let Ok(prefix) = prefix.parse::<u8>() else { return false };
    match (net, ip) {
        (IpAddr::V4(n), IpAddr::V4(i)) => {
            let mask = if prefix == 0 { 0 } else { !0u32 << (32 - prefix) };
            (u32::from(n) & mask) == (u32::from(i) & mask)
        }
        (IpAddr::V6(n), IpAddr::V6(i)) => {
            let mask = if prefix == 0 { 0 } else { !0u128 << (128 - prefix) };
            (u128::from(n) & mask) == (u128::from(i) & mask)
        }
        _ => false,
    }
}

/// 自定义规则判定: 返回 Some(RuleAction) 或 None (未命中)
fn custom_rules_verdict(domain: Option<&str>, ip: Option<IpAddr>) -> Option<RuleAction> {
    let rules = rules_store().read().unwrap_or_else(|e| e.into_inner());
    for (kind, action) in &rules.rules {
        let mut hit = false;
        if let Some(d) = domain {
            hit = match kind {
                RuleKind::Suffix(pat) => domain_match(pat, d),
                RuleKind::Exact(pat) => d.trim_end_matches('.').eq_ignore_ascii_case(pat.trim_end_matches('.')),
                RuleKind::Keyword(pat) => d.to_ascii_lowercase().contains(pat),
                RuleKind::Regex(_pat, re_opt) => re_opt.as_ref().map(|re| re.is_match(d)).unwrap_or(false),
                RuleKind::GeoSite(tag) => crate::geo::match_geosite_tag(tag, d),
                RuleKind::GeoIp(code) => {
                    if let Some(ip_addr) = ip {
                        crate::geo::match_geoip_code(code, ip_addr)
                    } else {
                        false
                    }
                }
                RuleKind::Cidr(cidr) => {
                    if let Some(ip_addr) = ip {
                        cidr_match(cidr, ip_addr)
                    } else {
                        false
                    }
                }
            };
        } else if let Some(ip_addr) = ip {
            hit = match kind {
                RuleKind::GeoIp(code) => crate::geo::match_geoip_code(code, ip_addr),
                RuleKind::Cidr(cidr) => cidr_match(cidr, ip_addr),
                _ => false,
            };
        }

        if hit {
            record_rule_hit(kind.kind_str(), kind.pattern_str(), action.as_str());
            return Some(*action);
        }
    }
    None
}

/// 综合路由判定: 返回 RuleAction (Direct / Proxy / Block)
pub fn should_route(domain: Option<&str>, ip: Option<IpAddr>) -> RuleAction {
    // ① 用户自定义规则 (最高优先级: 支持 domain/cidr/geosite/geoip 及 block 动作)
    if let Some(action) = custom_rules_verdict(domain, ip) {
        return action;
    }
    // ② 内置 CN 规则
    if let Some(d) = domain {
        if is_cn_domain(d) {
            return RuleAction::Direct;
        }
    }
    if let Some(ip) = ip {
        if is_direct_ip(ip) || is_cn_ip(ip) {
            return RuleAction::Direct;
        }
    }
    RuleAction::Proxy
}

/// 判断目标是否直连
pub fn should_direct(domain: Option<&str>, ip: Option<IpAddr>) -> bool {
    should_route(domain, ip) == RuleAction::Direct
}

/// 判断目标是否被阻断/拦截
pub fn should_block(domain: Option<&str>, ip: Option<IpAddr>) -> bool {
    should_route(domain, ip) == RuleAction::Block
}

/// 国内域名 → 真实 IP
pub fn resolve_direct_domain(domain: &str) -> Option<std::net::IpAddr> {
    crate::tun::dns::direct_dns_lookup(domain)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cn_ip_matches() {
        assert!(is_cn_ip("114.114.114.114".parse().unwrap()));
        assert!(is_cn_ip("223.5.5.5".parse().unwrap()));
        assert!(is_cn_ip("101.226.103.106".parse().unwrap()));
        assert!(!is_cn_ip("8.8.8.8".parse().unwrap()));
    }

    #[test]
    fn cn_domain_matches() {
        assert!(is_cn_domain("www.baidu.com"));
        assert!(is_cn_domain("mp.weixin.qq.com"));
        assert!(is_cn_domain("www.12306.cn"));
        assert!(!is_cn_domain("www.google.com"));
    }

    fn mk(rs: &str) -> String {
        format!(r#"{{"rules": {rs}}}"#)
    }

    #[test]
    fn custom_rules_with_block() {
        set_custom_rules(&mk(r#"[
            {"kind":"keyword","pattern":"adservice","action":"block"},
            {"kind":"suffix","pattern":"google.com","action":"direct"},
            {"kind":"cidr","pattern":"8.8.8.0/24","action":"block"}
        ]"#));

        assert_eq!(should_route(Some("adservice.google.com"), None), RuleAction::Block);
        assert!(should_block(Some("adservice.google.com"), None));

        assert_eq!(should_route(Some("mail.google.com"), None), RuleAction::Direct);
        assert!(should_direct(Some("mail.google.com"), None));

        assert_eq!(should_route(None, Some("8.8.8.8".parse().unwrap())), RuleAction::Block);
        assert_eq!(should_route(None, Some("1.1.1.1".parse().unwrap())), RuleAction::Proxy);
        set_custom_rules(r#"{"rules":[]}"#);
    }
}
