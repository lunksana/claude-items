//! DNS 应答器 + 国内外分流。
//!
//! TUN 内的 `dns_addr:53` 查询 (UDP/TCP) 处理:
//! - **国内域名** (见 `crate::direct::is_cn_domain`) → 异步向上游 DNS (223.5.5.5, protect
//!   UDP socket 走真实网络) 查询**真实 IP** → 应答 A 记录 → 客户端连真实 IP → 命中 CN 段 → 直连
//! - **国外域名** → 分配 fake-IP (198.18.0.0/16) → 客户端连 fake-IP → 隧道 (服务端远程解析)
//! - **AAAA** → 空 answer (NOERROR), 强制回落 IPv4
//!
//! 上游查询结果缓存 300s, `direct::resolve_direct_domain` 可同步读取兜底。

use smoltcp::iface::SocketHandle;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant};
use tracing::debug;

use crate::engine::Engine;
use crate::tun::TunStack;
use crate::tun::tcp::TunTcpStream;

/// 国内域名 → 真实 IP 共享缓存 (分流写入, direct::resolve_direct_domain 读)。
fn direct_cache() -> &'static StdMutex<HashMap<String, (std::net::Ipv4Addr, Instant)>> {
    static C: OnceLock<StdMutex<HashMap<String, (std::net::Ipv4Addr, Instant)>>> = OnceLock::new();
    C.get_or_init(|| StdMutex::new(HashMap::new()))
}

/// 上游 DNS (国内公共 DNS, protect socket 真实网络查询)。
const UPSTREAM_DNS: [u8; 4] = [223, 5, 5, 5];
const CACHE_TTL: Duration = Duration::from_secs(300);

/// 读直连 DNS 缓存 (同步, 兜底用)。
pub fn direct_dns_lookup(domain: &str) -> Option<std::net::IpAddr> {
    let map = direct_cache().lock().unwrap_or_else(|e| e.into_inner());
    let (ip, at) = map.get(domain)?;
    if at.elapsed() < CACHE_TTL {
        Some(std::net::IpAddr::V4(*ip))
    } else {
        None
    }
}

/// 构造标准 A 查询 (无 EDNS)。
fn build_a_query(domain: &str, id: u16) -> Vec<u8> {
    let mut q = Vec::with_capacity(40);
    q.extend_from_slice(&id.to_be_bytes());
    q.extend_from_slice(&[0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0]); // RD, 1 question
    for label in domain.trim_end_matches('.').split('.') {
        if label.is_empty() {
            continue;
        }
        q.push(label.len() as u8);
        q.extend_from_slice(label.as_bytes());
    }
    q.push(0);
    q.extend_from_slice(&[0, 1, 0, 1]); // A, IN
    q
}

/// 解析上游应答, 取第一个 A 记录。
fn parse_a_answer(buf: &[u8], qname_len: usize) -> Option<[u8; 4]> {
    if buf.len() < 12 {
        return None;
    }
    let ancount = u16::from_be_bytes([buf[6], buf[7]]);
    if ancount == 0 {
        return None;
    }
    let mut off = 12 + qname_len + 4;
    for _ in 0..ancount.min(8) {
        if off + 10 > buf.len() {
            return None;
        }
        let name_len = if buf[off] & 0xC0 == 0xC0 {
            2
        } else {
            let mut p = off;
            while p < buf.len() && buf[p] != 0 {
                p += 1 + buf[p] as usize;
            }
            p - off + 1
        };
        let r = off + name_len;
        if r + 10 > buf.len() {
            return None;
        }
        let rtype = u16::from_be_bytes([buf[r], buf[r + 1]]);
        let rdlen = u16::from_be_bytes([buf[r + 8], buf[r + 9]]) as usize;
        let rd = r + 10;
        if rd + rdlen > buf.len() {
            return None;
        }
        if rtype == 1 && rdlen == 4 {
            return Some([buf[rd], buf[rd + 1], buf[rd + 2], buf[rd + 3]]);
        }
        off = rd + rdlen;
    }
    None
}

/// 异步向上游查询真实 IP (protect UDP socket)。
async fn resolve_upstream(domain: &str) -> Option<std::net::Ipv4Addr> {
    {
        let map = direct_cache().lock().unwrap_or_else(|e| e.into_inner());
        if let Some((ip, at)) = map.get(domain) {
            if at.elapsed() < CACHE_TTL {
                return Some(*ip);
            }
        }
    }
    let sock = match tokio::net::UdpSocket::bind("0.0.0.0:0").await {
        Ok(s) => s,
        Err(_) => return None,
    };
    // protect: 走真实网络 (否则被 TUN 卷回)
    crate::protect::protect(std::os::unix::io::AsRawFd::as_raw_fd(&sock));
    static NEXT_ID: AtomicU16 = AtomicU16::new(0x9000);
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let query = build_a_query(domain, id);
    let up: std::net::SocketAddr =
        std::net::SocketAddr::from((std::net::Ipv4Addr::from(UPSTREAM_DNS), 53));
    if sock.send_to(&query, up).await.is_err() {
        return None;
    }
    let mut buf = [0u8; 512];
    let (n, _) = match tokio::time::timeout(Duration::from_secs(3), sock.recv_from(&mut buf)).await {
        Ok(Ok(v)) => v,
        _ => return None,
    };
    if n < 2 || u16::from_be_bytes([buf[0], buf[1]]) != id {
        return None;
    }
    // qname 长度
    let qname_end = {
        let mut p = 12usize;
        while p < n && buf[p] != 0 {
            p += 1 + buf[p] as usize;
        }
        p - 12 + 1
    };
    if let Some(ip) = parse_a_answer(&buf[..n], qname_end) {
        direct_cache()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(domain.to_string(), (std::net::Ipv4Addr::from(ip), Instant::now()));
        // 标记该 IP 为直连 (TCP 层对裸 IP 判定用, 保证 DNS/TCP 分流一致)
        crate::direct::mark_direct_ip(std::net::IpAddr::V4(std::net::Ipv4Addr::from(ip)));
        return Some(std::net::Ipv4Addr::from(ip));
    }
    None
}

/// 构造应答: A → 给定 IP; AAAA/其他 → 空 answer (NOERROR)。
fn build_response(query: &[u8], domain: &str, qtype: u16, a_record: Option<[u8; 4]>, question_len: usize) -> Option<Vec<u8>> {
    if query.len() < 12 || question_len < 13 || question_len > query.len() {
        return None;
    }
    let id = &query[0..2];
    let question = &query[12..question_len];

    let mut resp = Vec::with_capacity(12 + question.len() + 16);
    resp.extend_from_slice(id);
    resp.extend_from_slice(&[0x81, 0x80]);
    resp.extend_from_slice(&[0, 1]);
    if a_record.is_some() {
        resp.extend_from_slice(&[0, 1]);
    } else {
        resp.extend_from_slice(&[0, 0]);
    }
    resp.extend_from_slice(&[0, 0, 0, 0]);
    resp.extend_from_slice(question);

    if let Some(ip) = a_record {
        resp.extend_from_slice(&[0xC0, 0x0C]);
        resp.extend_from_slice(&qtype.to_be_bytes());
        resp.extend_from_slice(&[0, 1]); // IN
        // Fake-IP (198.18.0.0/16) 使用 1 秒极短 TTL，防止 VPN 断开后 App 仍残留 Fake-IP 导致无法联网
        let is_fake = ip[0] == 198 && ip[1] == 18;
        let ttl: u32 = if is_fake { 1 } else { 60 };
        resp.extend_from_slice(&ttl.to_be_bytes()); // TTL
        resp.extend_from_slice(&[0, 4]);
        resp.extend_from_slice(&ip);
    }
    let _ = domain;
    Some(resp)
}

/// 解析 DNS 查询, 返回 (QNAME, QTYPE, question 段长度)。
fn parse_query(buf: &[u8]) -> Option<(String, u16, usize)> {
    if buf.len() < 12 {
        return None;
    }
    let qdcount = u16::from_be_bytes([buf[4], buf[5]]);
    if qdcount < 1 {
        return None;
    }
    let mut off = 12usize;
    let mut labels = Vec::new();
    loop {
        let len = *buf.get(off)? as usize;
        off += 1;
        if len == 0 {
            break;
        }
        if len > 63 || off + len > buf.len() {
            return None;
        }
        labels.push(&buf[off..off + len]);
        off += len;
    }
    if off + 4 > buf.len() {
        return None;
    }
    let qtype = u16::from_be_bytes([buf[off], buf[off + 1]]);
    let mut name = String::new();
    for (i, l) in labels.iter().enumerate() {
        if i > 0 {
            name.push('.');
        }
        for &b in l.iter() {
            name.push(b as char);
        }
    }
    Some((name.to_ascii_lowercase(), qtype, off + 4))
}

/// 把应答写回 TUN 的公共封装。
fn send_dns_reply(stack: &TunStack, client: std::net::SocketAddr, query: &[u8],
                  domain: &str, qtype: u16, a: Option<[u8; 4]>, question_len: usize) {
    if let Some(resp) = build_response(query, domain, qtype, a, question_len) {
        let dns_addr = stack.dns_addr_std();
        let src = std::net::SocketAddr::new(std::net::IpAddr::V4(dns_addr), 53);
        if let Some(pkt) = crate::tun::udp::build_reply_ip_public(src, client, &resp) {
            stack.write_raw(&pkt);
        }
    }
}

/// DNS 查询计数 (流量监测用)。
pub static DNS_QUERIES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// DNS 查询入口 (mod.rs 调用): 分流 (国内→真实 IP, 国外→fake-IP)。
pub fn handle_dns_query(stack: Arc<TunStack>, client: std::net::SocketAddr, query: &[u8]) {
    use crate::direct;
    let Some((domain, qtype, question_len)) = parse_query(query) else { return };
    DNS_QUERIES.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    debug!("[TUN-DNS] 查询 {} (type {})", domain, qtype);

    let is_direct = qtype == 1 && direct::should_direct(Some(&domain), None);
    let cid = crate::monitor::record_conn_start(
        "DNS",
        &format!("{domain}:53"),
        if is_direct { "直连解析" } else { "Fake-IP 代理" },
    );

    if is_direct {
        // 直连域名 (内置国内 或 用户自定义规则) → 异步上游真实解析 (返回真实 IP, 直连用)
        let stack2 = stack.clone();
        let query2 = query.to_vec();
        let qlen = query.len() as u64;
        tokio::spawn(async move {
            match resolve_upstream(&domain).await {
                Some(ip) => {
                    send_dns_reply(&stack2, client, &query2, &domain, qtype, Some(ip.octets()), question_len);
                    crate::monitor::record_conn_close(cid, qlen, 64);
                }
                // 上游失败 → 兜底 fake-IP (保证连通性)
                None => {
                    if let Some(a) = stack2.engine().fake_ip_allocate(&domain).map(|i| i.octets()) {
                        send_dns_reply(&stack2, client, &query2, &domain, qtype, Some(a), question_len);
                        crate::monitor::record_conn_close(cid, qlen, 64);
                    } else {
                        crate::monitor::record_conn_close(cid, qlen, 0);
                    }
                }
            }
        });
        return;
    }

    // 默认: fake-IP
    let a = if qtype == 1 {
        stack.engine().fake_ip_allocate(&domain).map(|ip| ip.octets())
    } else {
        None
    };
    send_dns_reply(&stack, client, query, &domain, qtype, a, question_len);
    crate::monitor::record_conn_close(cid, query.len() as u64, 64);
}

/// UDP DNS 应答 (兼容旧接口, TCP DNS relay 用; fake-IP 路径)。
pub fn answer_dns_udp(engine: &Engine, payload: &[u8]) -> Option<Vec<u8>> {
    let (domain, qtype, question_len) = parse_query(payload)?;
    debug!("[TUN-DNS] 查询 {} (type {})", domain, qtype);
    let a = if qtype == 1 {
        engine.fake_ip_allocate(&domain).map(|ip| ip.octets())
    } else {
        None
    };
    build_response(payload, &domain, qtype, a, question_len)
}

/// TCP DNS relay: 读 [2B len][query] → 回 [2B len][response] → 关闭。
pub async fn relay_tcp_dns(stack: Arc<TunStack>, handle: SocketHandle) {
    let stream = match TunTcpStream::from_catcher(stack.clone(), handle).await {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut stream = stream;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    loop {
        let mut len_buf = [0u8; 2];
        if stream.read_exact(&mut len_buf).await.is_err() {
            break;
        }
        let qlen = u16::from_be_bytes(len_buf) as usize;
        if qlen == 0 || qlen > 4096 {
            break;
        }
        let mut query = vec![0u8; qlen];
        if stream.read_exact(&mut query).await.is_err() {
            break;
        }
        // TCP DNS 也走分流 (国内→真实 IP)
        if let Some((domain, qtype, question_len)) = parse_query(&query) {
            if qtype == 1 && crate::direct::should_direct(Some(&domain), None) {
                if let Some(ip) = resolve_upstream(&domain).await {
                    let resp = build_response(&query, &domain, qtype, Some(ip.octets()), question_len).unwrap_or_default();
                    let mut framed = Vec::with_capacity(2 + resp.len());
                    framed.extend_from_slice(&(resp.len() as u16).to_be_bytes());
                    framed.extend_from_slice(&resp);
                    if stream.write_all(&framed).await.is_err() {
                        break;
                    }
                    continue;
                }
            }
        }
        let resp = answer_dns_udp(&stack.engine(), &query).unwrap_or_default();
        let mut framed = Vec::with_capacity(2 + resp.len());
        framed.extend_from_slice(&(resp.len() as u16).to_be_bytes());
        framed.extend_from_slice(&resp);
        if stream.write_all(&framed).await.is_err() {
            break;
        }
    }
}
