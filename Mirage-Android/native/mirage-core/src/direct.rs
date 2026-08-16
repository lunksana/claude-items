//! 国内外分流: 目标判定 (直连 vs 代理)。
//!
//! 分流策略 (与主流代理 App 一致):
//! ```text
//! DNS 查询 → 国内域名 → 返回真实 IP (上游 DNS 查询) → 客户端连真实 IP
//!             国外域名 → 返回 fake-IP → 客户端连 fake-IP → 走隧道
//! TCP/UDP 目标:
//!   fake-IP        → 代理 (隧道)
//!   裸 IP, 命中 CN  → 直连 (protect socket 走真实网络)
//!   裸 IP, 非 CN    → 代理
//! ```
//!
//! 数据来源: `direct_cn_ipv4.rs` (提取自 Mirage-rs geoip.dat 的 CN 段, 7727 段合并);
//! `direct_cn_domains.rs` (国内常用域名白名单, 可增补)。

use std::net::IpAddr;

mod cn_ipv4 { include!("direct_cn_ipv4.rs"); }
mod cn_domains { include!("direct_cn_domains.rs"); }

/// 判断 IP 是否属于中国段 (线性扫描; 段数 ~7700, 每次连接判定一次, ARM 上 <100µs,
/// 连接建立频率远低于此, 无需优化; 若未来需要可换前缀树)。
pub fn is_cn_ip(ip: IpAddr) -> bool {
    let ip_u32 = match ip {
        IpAddr::V4(v4) => u32::from(v4),
        IpAddr::V6(_) => return false, // v6 暂不直连 (国内 v6 覆盖不完整, 保守走代理)
    };
    for (net, prefix) in cn_ipv4::CN_IPV4 {
        let mask = if *prefix == 0 { 0 } else { !0u32 << (32 - prefix) };
        if (ip_u32 & mask) == (*net & mask) {
            return true;
        }
    }
    false
}

/// 判断域名是否"国内" (命中白名单或国内后缀 → 直连)。
pub fn is_cn_domain(domain: &str) -> bool {
    let d = domain.trim_end_matches('.').to_ascii_lowercase();
    for suffix in cn_domains::CN_DOMAINS {
        if d == *suffix || d.ends_with(&format!(".{suffix}")) {
            return true;
        }
    }
    // 国内顶级后缀
    for tld in ["cn", "com.cn", "net.cn", "org.cn", "edu.cn", "gov.cn", "mil.cn"] {
        if d == tld || d.ends_with(&format!(".{tld}")) {
            return true;
        }
    }
    false
}

/// 域名匹配方式 (对齐 Clash 的 DOMAIN 系列):
/// - Suffix: DOMAIN-SUFFIX (example.com 匹配自身及所有子域)
/// - Exact:  DOMAIN (精确匹配, 不含子域)
/// - Keyword: DOMAIN-KEYWORD (任意位置包含)
/// - Regex:  DOMAIN-REGEX (正则, Rust regex 语法)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DomainMatch { Suffix, Exact, Keyword, Regex }

/// 用户自定义分流规则 (优先于内置 CN 规则)。由 App 的「分流规则」界面维护, 经 JNI
/// `setRules` 注入。线程安全: 运行时读, 界面改。
///
/// JSON 格式 (与 App 的 RuleStore 一致):
/// ```json
/// {"rules": [
///   {"kind": "suffix",  "pattern": "example.com", "action": "direct"},
///   {"kind": "exact",   "pattern": "blocked.io",  "action": "proxy"},
///   {"kind": "keyword", "pattern": "ads",         "action": "proxy"},
///   {"kind": "regex",   "pattern": "^.*\.cn$",   "action": "direct"},
///   {"kind": "cidr",    "pattern": "1.2.3.0/24",  "action": "direct"}
/// ]}
/// ```
#[derive(Default)]
pub struct CustomRules {
    /// (匹配方式, pattern, 是否直连)
    pub rules: Vec<(DomainMatch, String, bool)>,
    pub cidrs_direct: Vec<String>,
    pub cidrs_proxy: Vec<String>,
}

fn rules_store() -> &'static std::sync::RwLock<CustomRules> {
    use std::sync::OnceLock;
    static R: OnceLock<std::sync::RwLock<CustomRules>> = OnceLock::new();
    R.get_or_init(|| std::sync::RwLock::new(CustomRules::default()))
}

/// 直连 IP 集合: DNS 分流把"直连域名"解析出的真实 IP 记到这里, TCP 层对裸 IP 判定时
/// 优先查它 (命中 → 直连), 保证"用户规则/国内域名 → 直连"在 DNS 层与 TCP 层一致。
fn direct_ips() -> &'static std::sync::Mutex<std::collections::HashSet<IpAddr>> {
    use std::sync::OnceLock;
    static S: OnceLock<std::sync::Mutex<std::collections::HashSet<IpAddr>>> = OnceLock::new();
    S.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}

/// 标记某 IP 为直连 (DNS 分流调用)。
pub fn mark_direct_ip(ip: IpAddr) {
    direct_ips().lock().unwrap_or_else(|e| e.into_inner()).insert(ip);
}

/// 查询某 IP 是否被标记为直连。
pub fn is_direct_ip(ip: IpAddr) -> bool {
    direct_ips().lock().unwrap_or_else(|e| e.into_inner()).contains(&ip)
}

/// 内置国内域名列表 (只读导出, 规则界面展示用)。
pub fn builtin_domains() -> Vec<String> {
    cn_domains::CN_DOMAINS.iter().map(|s| s.to_string()).collect()
}

/// 内置中国 IP 段数量 (规则界面展示用)。
pub fn builtin_ip_count() -> usize {
    cn_ipv4::CN_IPV4.len()
}

/// 设置自定义规则 (JNI 调用, App 启动/改规则时注入)。解析失败返回 false。
pub fn set_custom_rules(json: &str) -> bool {
    let parsed: Result<serde_json::Value, _> = serde_json::from_str(json);
    let Ok(v) = parsed else { return false };
    let mut rules = CustomRules::default();

    // 新格式: {"rules":[{kind,pattern,action}]}
    if let Some(arr) = v.get("rules").and_then(|a| a.as_array()) {
        for item in arr {
            let (Some(kind), Some(pattern), Some(action)) =
                (item.get("kind").and_then(|x| x.as_str()),
                 item.get("pattern").and_then(|x| x.as_str()),
                 item.get("action").and_then(|x| x.as_str())) else { continue };
            let is_direct = action.eq_ignore_ascii_case("direct");
            match kind {
                "cidr" => {
                    if is_direct {
                        rules.cidrs_direct.push(pattern.to_string());
                    } else {
                        rules.cidrs_proxy.push(pattern.to_string());
                    }
                }
                "exact" => rules.rules.push((DomainMatch::Exact, pattern.to_ascii_lowercase(), is_direct)),
                "keyword" => rules.rules.push((DomainMatch::Keyword, pattern.to_ascii_lowercase(), is_direct)),
                "regex" => {
                    if regex::Regex::new(pattern).is_ok() {
                        rules.rules.push((DomainMatch::Regex, pattern.to_string(), is_direct));
                    }
                }
                _ => rules.rules.push((DomainMatch::Suffix, pattern.to_ascii_lowercase(), is_direct)),
            }
        }
    } else {
        // 旧格式兼容: domains_direct/domains_proxy/cidrs_direct/cidrs_proxy (后缀匹配)
        let get_list = |key: &str| -> Vec<String> {
            v.get(key).and_then(|a| a.as_array()).map(|arr| {
                arr.iter().filter_map(|x| x.as_str().map(|s| s.to_ascii_lowercase())).collect()
            }).unwrap_or_default()
        };
        for p in get_list("domains_direct") {
            rules.rules.push((DomainMatch::Suffix, p, true));
        }
        for p in get_list("domains_proxy") {
            rules.rules.push((DomainMatch::Suffix, p, false));
        }
        rules.cidrs_direct = get_list("cidrs_direct");
        rules.cidrs_proxy = get_list("cidrs_proxy");
    }
    *rules_store().write().unwrap_or_else(|e| e.into_inner()) = rules;
    true
}

fn domain_match(rule: &str, domain: &str) -> bool {
    let rule = rule.trim_end_matches('.').to_ascii_lowercase();
    let d = domain.trim_end_matches('.');
    d == rule || d.ends_with(&format!(".{rule}"))
}

fn cidr_match(rule: &str, ip: IpAddr) -> bool {
    let Some((net, prefix)) = rule.split_once('/') else {
        // 裸 IP 规则: 精确匹配
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

/// 单个域名规则是否命中 (按匹配方式)。
fn rule_matches(m: DomainMatch, pattern: &str, domain: &str) -> bool {
    match m {
        DomainMatch::Suffix => domain_match(pattern, domain),
        DomainMatch::Exact => {
            let p = pattern.trim_end_matches('.');
            let d = domain.trim_end_matches('.');
            d == p
        }
        DomainMatch::Keyword => domain.contains(pattern),
        DomainMatch::Regex => regex::Regex::new(pattern)
            .map(|re| re.is_match(domain))
            .unwrap_or(false),
    }
}

/// 自定义规则判定: None = 未命中; Some(true) = 直连; Some(false) = 代理。
/// 按规则列表顺序, 第一条命中即生效 (与 Clash 规则顺序语义一致)。
fn custom_rules_verdict(domain: Option<&str>, ip: Option<IpAddr>) -> Option<bool> {
    let rules = rules_store().read().unwrap_or_else(|e| e.into_inner());
    if let Some(d) = domain {
        for (m, pattern, is_direct) in &rules.rules {
            if rule_matches(*m, pattern, d) {
                return Some(*is_direct);
            }
        }
    }
    if let Some(ip) = ip {
        for r in &rules.cidrs_proxy {
            if cidr_match(r, ip) {
                return Some(false);
            }
        }
        for r in &rules.cidrs_direct {
            if cidr_match(r, ip) {
                return Some(true);
            }
        }
    }
    None
}

/// 综合判定目标是否直连。
/// 优先级: 用户自定义规则 → 内置 CN 规则 → 默认 (代理)。
/// - `domain`: fake-IP 反查出的域名 (有 → 用域名规则)
/// - `ip`: 裸 IP 目标
pub fn should_direct(domain: Option<&str>, ip: Option<IpAddr>) -> bool {
    // ① 用户自定义规则 (最高优先)
    if let Some(v) = custom_rules_verdict(domain, ip) {
        return v;
    }
    // ② 内置 CN 规则
    if let Some(d) = domain {
        return is_cn_domain(d);
    }
    if let Some(ip) = ip {
        // ① 直连标记 (来自 DNS 分流, 保证规则一致性) ② 内置 CN
        return is_direct_ip(ip) || is_cn_ip(ip);
    }
    false
}

/// 国内域名 → 真实 IP (兜底用; 主路径是 DNS 分流直接把国内域名解析为真实 IP,
/// 客户端连的就是真实 IP, 不会走到 fake-IP)。此处查共享的直连 DNS 缓存。
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
    }

    #[test]
    fn foreign_ip_not_cn() {
        assert!(!is_cn_ip("8.8.8.8".parse().unwrap()));
        assert!(!is_cn_ip("1.1.1.1".parse().unwrap()));
        assert!(!is_cn_ip("172.217.0.0".parse().unwrap()));
    }

    #[test]
    fn cn_domain_matches() {
        assert!(is_cn_domain("www.baidu.com"));
        assert!(is_cn_domain("mp.weixin.qq.com"));
        assert!(is_cn_domain("www.12306.cn"));
        assert!(is_cn_domain("sub.example.cn"));
    }

    #[test]
    fn foreign_domain_not_cn() {
        assert!(!is_cn_domain("www.google.com"));
        assert!(!is_cn_domain("www.apple.com"));
        assert!(!is_cn_domain("github.com"));
    }

    fn mk(rs: &str) -> String {
        format!(r#"{{"rules": {rs}}}"#)
    }

    #[test]
    fn custom_rules_override() {
        assert!(!should_direct(Some("www.google.com"), None));
        set_custom_rules(&mk(r#"[{"kind":"suffix","pattern":"google.com","action":"direct"}]"#));
        assert!(should_direct(Some("www.google.com"), None));
        // 自定义代理覆盖内置 CN
        set_custom_rules(&mk(r#"[{"kind":"suffix","pattern":"baidu.com","action":"proxy"}]"#));
        assert!(!should_direct(Some("www.baidu.com"), None));
        // IP 段规则
        set_custom_rules(r#"{"rules":[{"kind":"cidr","pattern":"9.9.9.0/24","action":"direct"}]}"#);
        assert!(should_direct(None, Some("9.9.9.9".parse().unwrap())));
        assert!(!should_direct(None, Some("8.8.8.8".parse().unwrap())));
        // 清空
        set_custom_rules(r#"{"rules":[]}"#);
        assert!(!should_direct(Some("www.google.com"), None));
        assert!(should_direct(Some("www.baidu.com"), None));
    }

    #[test]
    fn clash_domain_match_modes() {
        // exact: 只匹配自身, 不匹配子域
        set_custom_rules(&mk(r#"[{"kind":"exact","pattern":"example.com","action":"direct"}]"#));
        assert!(should_direct(Some("example.com"), None));
        assert!(!should_direct(Some("a.example.com"), None));
        // suffix: 匹配子域
        set_custom_rules(&mk(r#"[{"kind":"suffix","pattern":"example.com","action":"direct"}]"#));
        assert!(should_direct(Some("a.example.com"), None));
        // keyword
        set_custom_rules(&mk(r#"[{"kind":"keyword","pattern":"ads","action":"direct"}]"#));
        assert!(should_direct(Some("ads-tracker.example.com"), None));
        assert!(!should_direct(Some("clean.example.com"), None));
        // regex
        set_custom_rules(&mk(r#"[{"kind":"regex","pattern":"^.*\.cn$","action":"direct"}]"#));
        assert!(should_direct(Some("deep.example.cn"), None));
        assert!(!should_direct(Some("example.com"), None));
        // 顺序: 第一条命中
        set_custom_rules(&mk(r#"[{"kind":"keyword","pattern":"ads","action":"proxy"},{"kind":"keyword","pattern":"ads","action":"direct"}]"#));
        assert!(!should_direct(Some("ads.example.com"), None));
        set_custom_rules(r#"{"rules":[]}"#);
    }
}
