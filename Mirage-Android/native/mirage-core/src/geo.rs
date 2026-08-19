//! Geo 数据文件解析与规则集匹配器 (geosite.dat / geoip.dat)。
//!
//! 零外部依赖解析标准 v2ray-rules-dat / Loyalsoldier / v2fly 格式的 protobuf 数据集。

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::path::Path;
use std::sync::{OnceLock, RwLock};
use tracing::{info, warn};

#[derive(Debug, Clone)]
pub enum SiteDomain {
    Keyword(String),
    Regex(String, Option<regex::Regex>),
    Suffix(String),
    Exact(String),
}

impl SiteDomain {
    pub fn matches(&self, domain: &str) -> bool {
        let d = domain.trim_end_matches('.').to_ascii_lowercase();
        match self {
            SiteDomain::Exact(v) => d == *v,
            SiteDomain::Suffix(v) => d == *v || d.ends_with(&format!(".{v}")),
            SiteDomain::Keyword(v) => d.contains(v),
            SiteDomain::Regex(_, Some(re)) => re.is_match(&d),
            SiteDomain::Regex(raw, None) => d.contains(raw),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Ipv4Cidr {
    pub net: u32,
    pub mask: u32,
    pub prefix: u8,
}

impl Ipv4Cidr {
    pub fn new(ip: Ipv4Addr, prefix: u8) -> Self {
        let net = u32::from(ip);
        let mask = if prefix == 0 { 0 } else { !0u32 << (32 - prefix) };
        Self {
            net: net & mask,
            mask,
            prefix,
        }
    }

    pub fn contains(&self, ip: Ipv4Addr) -> bool {
        (u32::from(ip) & self.mask) == self.net
    }
}

#[derive(Default)]
pub struct GeoStore {
    /// geosite: tag (大写, 如 "CN", "GOOGLE", "CATEGORY-ADS-ALL") -> 域名匹配列表
    pub sites: HashMap<String, Vec<SiteDomain>>,
    /// geoip: code (大写, 如 "CN", "TELEGRAM", "PRIVATE") -> IPv4 网段列表
    pub ip_v4: HashMap<String, Vec<Ipv4Cidr>>,
    pub geosite_path: String,
    pub geoip_path: String,
}

impl GeoStore {
    pub fn match_geosite(&self, tag: &str, domain: &str) -> bool {
        let tag_upper = tag.trim().to_ascii_uppercase();
        if let Some(list) = self.sites.get(&tag_upper) {
            for item in list {
                if item.matches(domain) {
                    return true;
                }
            }
        }
        false
    }

    pub fn match_geoip(&self, code: &str, ip: IpAddr) -> bool {
        let code_upper = code.trim().to_ascii_uppercase();
        match ip {
            IpAddr::V4(v4) => {
                if let Some(list) = self.ip_v4.get(&code_upper) {
                    for cidr in list {
                        if cidr.contains(v4) {
                            return true;
                        }
                    }
                }
            }
            IpAddr::V6(_) => {}
        }
        false
    }
}

fn global_geo() -> &'static RwLock<GeoStore> {
    static G: OnceLock<RwLock<GeoStore>> = OnceLock::new();
    G.get_or_init(|| RwLock::new(GeoStore::default()))
}

// ── 简易 Protobuf Wire 解码器 ────────────────────────────────────────────────

fn read_varint(buf: &[u8], mut pos: usize) -> Option<(u64, usize)> {
    let mut result = 0u64;
    let mut shift = 0;
    while pos < buf.len() {
        let b = buf[pos];
        pos += 1;
        result |= ((b & 0x7F) as u64) << shift;
        if (b & 0x80) == 0 {
            return Some((result, pos));
        }
        shift += 7;
        if shift >= 64 {
            return None;
        }
    }
    None
}

fn read_len_delim<'a>(buf: &'a [u8], pos: usize) -> Option<(&'a [u8], usize)> {
    let (len, pos) = read_varint(buf, pos)?;
    let len = len as usize;
    if pos + len > buf.len() {
        return None;
    }
    Some((&buf[pos..pos + len], pos + len))
}

// ── geosite.dat 解析 ─────────────────────────────────────────────────────────

fn parse_domain_msg(buf: &[u8]) -> Option<SiteDomain> {
    let mut pos = 0;
    let mut dtype = 0u64;
    let mut value = String::new();

    while pos < buf.len() {
        let (tag, next_pos) = read_varint(buf, pos)?;
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        match (fn_num, wire_type) {
            (1, 0) => {
                // type
                let (v, next_pos) = read_varint(buf, pos)?;
                dtype = v;
                pos = next_pos;
            }
            (2, 2) => {
                // value
                let (data, next_pos) = read_len_delim(buf, pos)?;
                value = String::from_utf8_lossy(data).trim_end_matches('.').to_ascii_lowercase();
                pos = next_pos;
            }
            (_, 0) => {
                let (_, next_pos) = read_varint(buf, pos)?;
                pos = next_pos;
            }
            (_, 2) => {
                let (_, next_pos) = read_len_delim(buf, pos)?;
                pos = next_pos;
            }
            _ => break,
        }
    }

    if value.is_empty() {
        return None;
    }

    match dtype {
        0 => Some(SiteDomain::Keyword(value)),
        1 => {
            let compiled = regex::Regex::new(&value).ok();
            Some(SiteDomain::Regex(value, compiled))
        }
        2 => Some(SiteDomain::Suffix(value)),
        3 => Some(SiteDomain::Exact(value)),
        _ => Some(SiteDomain::Suffix(value)),
    }
}

fn parse_geosite_entry(buf: &[u8]) -> Option<(String, Vec<SiteDomain>)> {
    let mut pos = 0;
    let mut code = String::new();
    let mut domains = Vec::new();

    while pos < buf.len() {
        let (tag, next_pos) = read_varint(buf, pos)?;
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        match (fn_num, wire_type) {
            (1, 2) => {
                // country_code
                let (data, next_pos) = read_len_delim(buf, pos)?;
                code = String::from_utf8_lossy(data).trim().to_ascii_uppercase();
                pos = next_pos;
            }
            (2, 2) => {
                // domain message
                let (data, next_pos) = read_len_delim(buf, pos)?;
                if let Some(d) = parse_domain_msg(data) {
                    domains.push(d);
                }
                pos = next_pos;
            }
            (_, 0) => {
                let (_, next_pos) = read_varint(buf, pos)?;
                pos = next_pos;
            }
            (_, 2) => {
                let (_, next_pos) = read_len_delim(buf, pos)?;
                pos = next_pos;
            }
            _ => break,
        }
    }

    if code.is_empty() {
        None
    } else {
        Some((code, domains))
    }
}

pub fn parse_geosite_file(data: &[u8]) -> HashMap<String, Vec<SiteDomain>> {
    let mut map = HashMap::new();
    let mut pos = 0;
    while pos < data.len() {
        let Some((tag, next_pos)) = read_varint(data, pos) else { break };
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        if fn_num == 1 && wire_type == 2 {
            let Some((entry_data, next_pos)) = read_len_delim(data, pos) else { break };
            if let Some((code, domains)) = parse_geosite_entry(entry_data) {
                map.insert(code, domains);
            }
            pos = next_pos;
        } else if wire_type == 0 {
            let Some((_, next_pos)) = read_varint(data, pos) else { break };
            pos = next_pos;
        } else if wire_type == 2 {
            let Some((_, next_pos)) = read_len_delim(data, pos) else { break };
            pos = next_pos;
        } else {
            break;
        }
    }
    map
}

// ── geoip.dat 解析 ───────────────────────────────────────────────────────────

fn parse_cidr_msg(buf: &[u8]) -> Option<Ipv4Cidr> {
    let mut pos = 0;
    let mut ip_bytes = Vec::new();
    let mut prefix = 0u8;

    while pos < buf.len() {
        let (tag, next_pos) = read_varint(buf, pos)?;
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        match (fn_num, wire_type) {
            (1, 2) => {
                let (data, next_pos) = read_len_delim(buf, pos)?;
                ip_bytes = data.to_vec();
                pos = next_pos;
            }
            (2, 0) => {
                let (v, next_pos) = read_varint(buf, pos)?;
                prefix = (v as u8).min(32);
                pos = next_pos;
            }
            (_, 0) => {
                let (_, next_pos) = read_varint(buf, pos)?;
                pos = next_pos;
            }
            (_, 2) => {
                let (_, next_pos) = read_len_delim(buf, pos)?;
                pos = next_pos;
            }
            _ => break,
        }
    }

    if ip_bytes.len() == 4 {
        let ip = Ipv4Addr::new(ip_bytes[0], ip_bytes[1], ip_bytes[2], ip_bytes[3]);
        Some(Ipv4Cidr::new(ip, prefix))
    } else {
        None
    }
}

fn parse_geoip_entry(buf: &[u8]) -> Option<(String, Vec<Ipv4Cidr>)> {
    let mut pos = 0;
    let mut code = String::new();
    let mut cidrs = Vec::new();

    while pos < buf.len() {
        let (tag, next_pos) = read_varint(buf, pos)?;
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        match (fn_num, wire_type) {
            (1, 2) => {
                let (data, next_pos) = read_len_delim(buf, pos)?;
                code = String::from_utf8_lossy(data).trim().to_ascii_uppercase();
                pos = next_pos;
            }
            (2, 2) => {
                let (data, next_pos) = read_len_delim(buf, pos)?;
                if let Some(c) = parse_cidr_msg(data) {
                    cidrs.push(c);
                }
                pos = next_pos;
            }
            (_, 0) => {
                let (_, next_pos) = read_varint(buf, pos)?;
                pos = next_pos;
            }
            (_, 2) => {
                let (_, next_pos) = read_len_delim(buf, pos)?;
                pos = next_pos;
            }
            _ => break,
        }
    }

    if code.is_empty() {
        None
    } else {
        Some((code, cidrs))
    }
}

pub fn parse_geoip_file(data: &[u8]) -> HashMap<String, Vec<Ipv4Cidr>> {
    let mut map = HashMap::new();
    let mut pos = 0;
    while pos < data.len() {
        let Some((tag, next_pos)) = read_varint(data, pos) else { break };
        pos = next_pos;
        let fn_num = tag >> 3;
        let wire_type = tag & 7;

        if fn_num == 1 && wire_type == 2 {
            let Some((entry_data, next_pos)) = read_len_delim(data, pos) else { break };
            if let Some((code, cidrs)) = parse_geoip_entry(entry_data) {
                map.insert(code, cidrs);
            }
            pos = next_pos;
        } else if wire_type == 0 {
            let Some((_, next_pos)) = read_varint(data, pos) else { break };
            pos = next_pos;
        } else if wire_type == 2 {
            let Some((_, next_pos)) = read_len_delim(data, pos) else { break };
            pos = next_pos;
        } else {
            break;
        }
    }
    map
}

// ── 公共加载与匹配接口 ───────────────────────────────────────────────────────

/// 加载外部 Geo 文件 (geosite.dat 与 geoip.dat)
pub fn load_geo_files(geosite_path: &str, geoip_path: &str) -> (usize, usize) {
    let mut site_map = HashMap::new();
    let mut ip_map = HashMap::new();

    if !geosite_path.is_empty() && Path::new(geosite_path).exists() {
        match std::fs::read(geosite_path) {
            Ok(bytes) => {
                site_map = parse_geosite_file(&bytes);
                info!("[GEO] 成功加载 geosite.dat ({} tags, 路径: {})", site_map.len(), geosite_path);
            }
            Err(e) => warn!("[GEO] 读取 geosite.dat 失败 ({}): {}", geosite_path, e),
        }
    }

    if !geoip_path.is_empty() && Path::new(geoip_path).exists() {
        match std::fs::read(geoip_path) {
            Ok(bytes) => {
                ip_map = parse_geoip_file(&bytes);
                info!("[GEO] 成功加载 geoip.dat ({} codes, 路径: {})", ip_map.len(), geoip_path);
            }
            Err(e) => warn!("[GEO] 读取 geoip.dat 失败 ({}): {}", geoip_path, e),
        }
    }

    let site_count = site_map.len();
    let ip_count = ip_map.len();

    let mut g = global_geo().write().unwrap_or_else(|e| e.into_inner());
    g.sites = site_map;
    g.ip_v4 = ip_map;
    g.geosite_path = geosite_path.to_string();
    g.geoip_path = geoip_path.to_string();

    (site_count, ip_count)
}

/// 匹配域名是否命中指定 geosite tag
pub fn match_geosite_tag(tag: &str, domain: &str) -> bool {
    let g = global_geo().read().unwrap_or_else(|e| e.into_inner());
    g.match_geosite(tag, domain)
}

/// 匹配 IP 是否命中指定 geoip code
pub fn match_geoip_code(code: &str, ip: IpAddr) -> bool {
    let g = global_geo().read().unwrap_or_else(|e| e.into_inner());
    g.match_geoip(code, ip)
}

/// 获取当前已加载的 GeoSite tags 和 GeoIP codes 列表 (按字母排序 JSON)
pub fn get_geo_tags_json() -> String {
    let g = global_geo().read().unwrap_or_else(|e| e.into_inner());
    let mut sites: Vec<String> = g.sites.keys().cloned().collect();
    let mut ips: Vec<String> = g.ip_v4.keys().cloned().collect();
    sites.sort();
    ips.sort();

    serde_json::json!({
        "geosite_count": sites.len(),
        "geoip_count": ips.len(),
        "geosite_tags": sites,
        "geoip_codes": ips,
    }).to_string()
}
