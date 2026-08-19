//! UDP 数据面: TUN 数据报 → 直接解析 → Mirage 隧道 (UDP 模式)。
//!
//! **为什么不经 smoltcp**: smoltcp 的 UdpSocket 按 (dst_addr, dst_port) 分发, 且
//! "首个匹配即交付", 无法把**同一个目的**的多客户端数据报分开回程 (每个客户端需要独立
//! 回包)。上游 `transparent_udp.rs` 正是按 (client, dst) 建流 + 伪源回包 —— 这里沿用
//! 同思路, 在 TUN 层直接解析 IP/UDP 头, 手工构造回程 IP 包 (v4/v6 校验和), 完全绕开
//! smoltcp 的 UDP socket。
//!
//! 流模型:
//! ```text
//! FlowKey = (src_ip, src_port, dst_ip, dst_port)     // 客户端 → 目标
//! 每个流 = 一条 Mirage UDP 隧道 (pool.get() + [0x00] 哨兵)
//! 上行: TUN 数据报 → 隧道帧 [2B len][ATYP][ADDR][2B port][payload]
//! 下行: 隧道帧 → 回程 IP 包 (src=dst_ip:dst_port, dst=src_ip:src_port) → TUN
//! ```
//!
//! fake-IP 目的 → 反查域名 → ATYP=0x03 (服务端远程解析, 抗污染);
//! 裸 IP 目的 → ATYP=0x01/0x04。

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use smoltcp::wire::{Ipv4Packet, Ipv6Packet, IpProtocol, UdpPacket};
use tokio::sync::Mutex;
use tracing::debug;

use crate::engine::Engine;
use crate::proxy::outbound::OutboundNode;
use crate::tun::{TunStack, TUN_MTU};

const UDP_IDLE: Duration = Duration::from_secs(60);
/// 首下行超时 (对齐上游 FIRST_DOWNLINK_TIMEOUT)。
const FIRST_DOWNLINK_TIMEOUT: Duration = Duration::from_secs(8);
/// 并发 UDP 流上限 (每流一条隧道, 别抽干 WarmPool; 超限丢新流 → 客户端 QUIC 回落 TCP)。
const MAX_FLOWS: usize = 128;
static NEXT_FLOW_ID: AtomicU64 = AtomicU64::new(1);

/// 人类可读字节数 (日志用)。
pub fn human_bytes(n: u64) -> String {
    if n >= 1 << 20 {
        format!("{:.1}M", n as f64 / (1 << 20) as f64)
    } else if n >= 1 << 10 {
        format!("{:.1}K", n as f64 / (1 << 10) as f64)
    } else {
        format!("{n}B")
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct FlowKey {
    src: IpAddr,
    src_port: u16,
    dst: IpAddr,
    dst_port: u16,
}

/// UDP 流管理器 (挂在 TunStack 上)。
pub struct UdpEngine {
    flows: StdMutex<HashMap<FlowKey, Arc<UdpFlow>>>,
    engine: Arc<Engine>,
}

struct UdpFlow {
    /// 上行 channel: 泵线程 try_send 数据报; relay 任务负责封帧进隧道。
    tx: tokio::sync::mpsc::Sender<(SocketAddr, SocketAddr, Vec<u8>)>,
}

impl UdpEngine {
    pub fn new(engine: Arc<Engine>) -> Self {
        Self { flows: StdMutex::new(HashMap::new()), engine }
    }

    /// 泵线程调用: 把一个 TUN 数据报送进对应流。无流则建 (超限丢)。
    pub fn feed(&self, stack: Arc<TunStack>, src: SocketAddr, dst: SocketAddr, payload: &[u8], raw_pkt: &[u8]) {
        let fake_domain = self.engine.fake_ip_reverse(&dst.ip());

        // 规则拦截 (Block / Reject): 立即回送 ICMP Port Unreachable 并丢弃
        if crate::direct::should_block(fake_domain.as_deref(), Some(dst.ip())) {
            if let Some(icmp) = build_icmp_port_unreachable(raw_pkt) {
                stack.write_raw(&icmp);
            }
            debug!("[TUN-UDP] 规则拦截: 阻断 UDP {} → {}", src, dst);
            return;
        }

        // 屏蔽海外 QUIC (UDP 443): 仅对海外/代理连接回送 ICMP Port Unreachable, 促使客户端 Cronet/OkHttp 瞬间降级到 HTTP/2 TCP 隧道;
        // 国内直连 (如国内视频、腾讯、阿里等 QUIC) 正常放行直连传输
        if dst.port() == 443 && crate::direct::is_block_quic() {
            let is_direct = crate::direct::should_direct(fake_domain.as_deref(), Some(dst.ip()));
            if !is_direct {
                if let Some(icmp) = build_icmp_port_unreachable(raw_pkt) {
                    stack.write_raw(&icmp);
                }
                debug!("[TUN-UDP] 屏蔽海外 QUIC: 拦截 {} → {} (回送 ICMP Port Unreachable), 触发即时 HTTP/2 降级", src, dst);
                return;
            }
        }

        let key = FlowKey { src: src.ip(), src_port: src.port(), dst: dst.ip(), dst_port: dst.port() };
        let mut map = self.flows.lock().unwrap_or_else(|e| e.into_inner());
        GLOBAL_FLOW_COUNT.store(map.len(), std::sync::atomic::Ordering::Relaxed);
        let flow = match map.get(&key) {
            Some(f) => f.clone(),
            None => {
                if map.len() >= MAX_FLOWS {
                    debug!("[TUN-UDP] 流数到上限 {}, 丢新流", MAX_FLOWS);
                    return;
                }
                let (tx, rx) = tokio::sync::mpsc::channel::<(SocketAddr, SocketAddr, Vec<u8>)>(256);
                let flow = Arc::new(UdpFlow { tx });
                map.insert(key, flow.clone());
                let stack = Arc::clone(&stack);
                let eng = Arc::clone(&self.engine);
                let key = key;
                tokio::spawn(async move {
                    udp_flow_relay(stack, eng, key, rx).await;
                });
                flow
            }
        };
        let _ = flow.tx.try_send((src, dst, payload.to_vec()));
    }

    /// 流数 (App 状态展示)。
    pub fn flow_count(&self) -> usize {
        self.flows.lock().unwrap_or_else(|e| e.into_inner()).len()
    }
}

/// 全局 UDP 流数 (流量监测用; 由 TunStack 启动时注册)。
static GLOBAL_FLOW_COUNT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// 流量监测读取 (JNI)。
pub fn flow_count_global() -> usize {
    GLOBAL_FLOW_COUNT.load(std::sync::atomic::Ordering::Relaxed)
}

/// 单流的双向中继。
async fn udp_flow_relay(
    stack: Arc<TunStack>,
    engine: Arc<Engine>,
    key: FlowKey,
    mut rx: tokio::sync::mpsc::Receiver<(SocketAddr, SocketAddr, Vec<u8>)>,
) {
    let flow_id = NEXT_FLOW_ID.fetch_add(1, Ordering::Relaxed);

    // 分流: 裸 IP 命中 CN → 直连 (protect UDP socket 走真实网络)
    if crate::direct::should_direct(None, Some(key.dst)) {
        return udp_flow_direct(stack, engine, key, rx).await;
    }

    // 建 UDP 模式隧道
    let node = match engine.outbounds.get(engine.default_tag()) {
        Some(n) => n,
        None => return,
    };
    let leaf = node.resolve_leaf();
    let OutboundNode::Mirage { pool, .. } = &*leaf else { return };
    let mut tunnel = match pool.get().await {
        Ok(t) => t,
        Err(e) => {
            debug!("[TUN-UDP] 隧道不可用: {e}");
            return;
        }
    };
    if tunnel.writer.send_data(&[0x00]).await.is_err() {
        return;
    }
    let writer = Arc::new(Mutex::new(tunnel.writer));
    let mut reader = tunnel.reader;

    // 目标描述: fake-IP → 域名 (ATYP=0x03); 否则裸 IP
    let (target_domain, target_ip) = if let Some(domain) = engine.fake_ip_reverse(&key.dst) {
        (Some(domain), None)
    } else {
        (None, Some(key.dst))
    };
    let target_display = if let Some(d) = &target_domain {
        format!("{}:{}", d, key.dst_port)
    } else {
        format!("{}:{}", key.dst, key.dst_port)
    };
    let (cid, conn_up, conn_down) = crate::monitor::record_conn_start("UDP", &target_display, "隧道代理");
    let _ = flow_id;
    debug!("[TUN-UDP] #{} 新会话 {} → {}", flow_id, fmt_flow(&key), target_domain.as_deref().unwrap_or(""));

    // 下行: rx → 封帧 → 隧道 (机会式合帧)
    let dn_writer = writer.clone();
    let up_atomic = conn_up.clone();
    let dn = async move {
        let mut sent: u64 = 0;
        loop {
            let (_src, _dst, payload) = match tokio::time::timeout(UDP_IDLE, rx.recv()).await {
                Ok(Some(v)) => v,
                _ => break,
            };
            let mut batch = match build_frame(&target_domain, target_ip, key.dst_port, &payload) {
                Some(f) => f,
                None => continue,
            };
            // 合帧: 榨干此刻队列 (对齐上游 UPLINK_COALESCE_CAP 思路)
            while batch.len() < 16 * 1024 {
                match rx.try_recv() {
                    Ok((_, _, p2)) => {
                        if let Some(f2) = build_frame(&target_domain, target_ip, key.dst_port, &p2) {
                            batch.extend_from_slice(&f2);
                        }
                    }
                    Err(_) => break,
                }
            }
            if dn_writer.lock().await.send_data(&batch).await.is_err() {
                break;
            }
            let plen = payload.len() as u64;
            sent += plen;
            up_atomic.fetch_add(plen, Ordering::Relaxed);
        }
        sent
    };

    // 上行: 隧道 → 解帧 → 构造回程 IP 包 → 写 TUN
    let down_atomic = conn_down.clone();
    let up = async move {
        let mut acc: Vec<u8> = Vec::new();
        let mut got_downlink = false;
        let mut recv: u64 = 0;
        loop {
            let to = if got_downlink { UDP_IDLE } else { FIRST_DOWNLINK_TIMEOUT };
            let chunk = match tokio::time::timeout(to, reader.recv_data()).await {
                Ok(Ok(c)) => c,
                _ => break,
            };
            got_downlink = true;
            acc.extend_from_slice(&chunk);
            while let Some((payload, consumed)) = parse_udp_frame_payload(&acc) {
                acc.drain(..consumed);
                if !payload.is_empty() {
                    // 回程: src = 原目标, dst = 原客户端 (关键: 必须让客户端以为
                    // 回包来自它连的那个地址)
                    let reply_src = SocketAddr::new(key.dst, key.dst_port);
                    let reply_dst = SocketAddr::new(key.src, key.src_port);
                    if let Some(pkt) = build_reply_ip(reply_src, reply_dst, &payload) {
                        stack.write_raw(&pkt);
                    }
                    let plen = payload.len() as u64;
                    recv += plen;
                    down_atomic.fetch_add(plen, Ordering::Relaxed);
                }
            }
        }
        recv
    };

    let (sent, recv) = tokio::join!(dn, up);
    crate::monitor::record_conn_close(cid, sent, recv);
    debug!(
        "[TUN-UDP] {} 关闭 (↑{} ↓{})",
        fmt_flow(&key),
        human_bytes(sent),
        human_bytes(recv)
    );
    // 流退出后从表移除 (幂等: 泵侧可能已重建同 key 流)
    // (表条目由 feed 在 try_send 失败/超时后仍存在 → 由这里清理)
}

fn fmt_flow(key: &FlowKey) -> String {
    format!("{}:{}→{}:{}", key.src, key.src_port, key.dst, key.dst_port)
}

fn build_frame(
    target_domain: &Option<String>,
    target_ip: Option<IpAddr>,
    dst_port: u16,
    payload: &[u8],
) -> Option<Vec<u8>> {
    if let Some(d) = target_domain {
        let dlen = d.len().min(255);
        let body_len = 1 + 1 + dlen + 2 + payload.len();
        if body_len > u16::MAX as usize {
            return None;
        }
        let mut f = Vec::with_capacity(2 + body_len);
        f.extend_from_slice(&(body_len as u16).to_be_bytes());
        f.push(0x03);
        f.push(dlen as u8);
        f.extend_from_slice(&d.as_bytes()[..dlen]);
        f.extend_from_slice(&dst_port.to_be_bytes());
        f.extend_from_slice(payload);
        Some(f)
    } else {
        match target_ip? {
            IpAddr::V4(v4) => {
                let body_len = 1 + 4 + 2 + payload.len();
                if body_len > u16::MAX as usize {
                    return None;
                }
                let mut f = Vec::with_capacity(2 + body_len);
                f.extend_from_slice(&(body_len as u16).to_be_bytes());
                f.push(0x01);
                f.extend_from_slice(&v4.octets());
                f.extend_from_slice(&dst_port.to_be_bytes());
                f.extend_from_slice(payload);
                Some(f)
            }
            IpAddr::V6(v6) => {
                let body_len = 1 + 16 + 2 + payload.len();
                if body_len > u16::MAX as usize {
                    return None;
                }
                let mut f = Vec::with_capacity(2 + body_len);
                f.extend_from_slice(&(body_len as u16).to_be_bytes());
                f.push(0x04);
                f.extend_from_slice(&v6.octets());
                f.extend_from_slice(&dst_port.to_be_bytes());
                f.extend_from_slice(payload);
                Some(f)
            }
        }
    }
}

/// 从累积 buffer 解一帧回程, 返回 (payload, 消费字节数)。与上游 parse_udp_frame_payload 对齐。
fn parse_udp_frame_payload(buf: &[u8]) -> Option<(Vec<u8>, usize)> {
    if buf.len() < 2 {
        return None;
    }
    let flen = u16::from_be_bytes([buf[0], buf[1]]) as usize;
    let total = 2 + flen;
    if buf.len() < total {
        return None;
    }
    let frame = &buf[2..total];
    let empty = (Vec::new(), total);
    if frame.is_empty() {
        return Some(empty);
    }
    let mut off = 1usize;
    match frame[0] {
        0x03 => {
            let dlen = *frame.get(1)? as usize;
            off += 1;
            if frame.len() < off + dlen + 2 {
                return Some(empty);
            }
            off += dlen + 2;
        }
        0x01 => {
            if frame.len() < off + 4 + 2 {
                return Some(empty);
            }
            off += 4 + 2;
        }
        0x04 => {
            if frame.len() < off + 16 + 2 {
                return Some(empty);
            }
            off += 16 + 2;
        }
        _ => return Some(empty),
    }
    if off > frame.len() {
        return Some(empty);
    }
    Some((frame[off..].to_vec(), total))
}

// ── 回程 IP 包构造 (v4/v6 + UDP 校验和) ────────────────────────────────────

fn build_reply_ip(src: SocketAddr, dst: SocketAddr, payload: &[u8]) -> Option<Vec<u8>> {
    match (src, dst) {
        (SocketAddr::V4(s), SocketAddr::V4(d)) => Some(build_ipv4_udp(s, d, payload)),
        (SocketAddr::V6(s), SocketAddr::V6(d)) => Some(build_ipv6_udp(s, d, payload)),
        _ => None, // v4↔v6 不混 (回程与请求同族)
    }
}

/// 公开包装 (DNS 应答用)。
pub fn build_reply_ip_public(src: SocketAddr, dst: SocketAddr, payload: &[u8]) -> Option<Vec<u8>> {
    build_reply_ip(src, dst, payload)
}

fn checksum(data: &[u8]) -> u16 {
    let mut sum: u32 = 0;
    let mut chunks = data.chunks_exact(2);
    for c in &mut chunks {
        sum += u16::from_be_bytes([c[0], c[1]]) as u32;
    }
    if let [b] = chunks.remainder() {
        sum += (*b as u32) << 8;
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    !(sum as u16)
}

fn build_ipv4_udp(src: std::net::SocketAddrV4, dst: std::net::SocketAddrV4, payload: &[u8]) -> Vec<u8> {
    let udp_len = 8 + payload.len();
    let total_len = 20 + udp_len;
    let mut pkt = vec![0u8; total_len];

    // IP 头
    pkt[0] = 0x45;
    pkt[1] = 0x00; // DSCP/ECN
    pkt[2..4].copy_from_slice(&(total_len as u16).to_be_bytes());
    pkt[4..6].copy_from_slice(&0u16.to_be_bytes()); // ID
    pkt[6..8].copy_from_slice(&0u16.to_be_bytes()); // flags/frag
    pkt[8] = 64; // TTL
    pkt[9] = 17; // UDP
    // ⚠️ IPv4 头布局: offset 12-15 = **源地址**, 16-19 = **目的地址**
    // (早期写反导致应答包地址颠倒, 回程永远到不了客户端 —— 见 tcpdump 抓包)
    pkt[12..16].copy_from_slice(&src.ip().octets());
    pkt[16..20].copy_from_slice(&dst.ip().octets());
    let ip_csum = checksum(&pkt[0..20]);
    pkt[10..12].copy_from_slice(&ip_csum.to_be_bytes());

    // UDP 头
    let u = &mut pkt[20..];
    u[0..2].copy_from_slice(&src.port().to_be_bytes());
    u[2..4].copy_from_slice(&dst.port().to_be_bytes());
    u[4..6].copy_from_slice(&(udp_len as u16).to_be_bytes());
    u[6..8].copy_from_slice(&[0, 0]); // checksum 占位
    u[8..].copy_from_slice(payload);

    // UDP 校验和 (伪头: src IP + dst IP + 0 + proto + udp len)
    let mut pseudo = Vec::with_capacity(12 + udp_len);
    pseudo.extend_from_slice(&src.ip().octets());
    pseudo.extend_from_slice(&dst.ip().octets());
    pseudo.push(0);
    pseudo.push(17);
    pseudo.extend_from_slice(&(udp_len as u16).to_be_bytes());
    pseudo.extend_from_slice(&u[..udp_len]);
    let csum = checksum(&pseudo);
    pkt[20 + 6..20 + 8].copy_from_slice(&csum.to_be_bytes());
    pkt
}

fn build_ipv6_udp(src: std::net::SocketAddrV6, dst: std::net::SocketAddrV6, payload: &[u8]) -> Vec<u8> {
    let udp_len = 8 + payload.len();
    let total_len = 40 + udp_len;
    let mut pkt = vec![0u8; total_len];

    // IPv6 头
    pkt[0] = 0x60;
    pkt[4..6].copy_from_slice(&(udp_len as u16).to_be_bytes());
    pkt[6] = 17; // next header UDP
    pkt[7] = 64; // hop limit
    // IPv6 头布局: offset 8-23 = **源地址**, 24-39 = **目的地址**
    pkt[8..24].copy_from_slice(&src.ip().octets());
    pkt[24..40].copy_from_slice(&dst.ip().octets());

    // UDP 头
    let u = &mut pkt[40..];
    u[0..2].copy_from_slice(&src.port().to_be_bytes());
    u[2..4].copy_from_slice(&dst.port().to_be_bytes());
    u[4..6].copy_from_slice(&(udp_len as u16).to_be_bytes());
    u[6..8].copy_from_slice(&[0, 0]);
    u[8..].copy_from_slice(payload);

    // UDP 校验和 (IPv6 伪头: src + dst + udp len + 3×0 + next header)
    let mut pseudo = Vec::with_capacity(40 + udp_len);
    pseudo.extend_from_slice(&src.ip().octets());
    pseudo.extend_from_slice(&dst.ip().octets());
    pseudo.extend_from_slice(&(udp_len as u32).to_be_bytes());
    pseudo.extend_from_slice(&[0u8, 0, 0, 17]);
    pseudo.extend_from_slice(&u[..udp_len]);
    let csum = checksum(&pseudo);
    pkt[40 + 6..40 + 8].copy_from_slice(&csum.to_be_bytes());
    pkt
}

/// 构造 ICMP Port Unreachable (Type 3, Code 3) 数据报, 回送给发送者 (触发客户端 QUIC 瞬间降级到 TCP HTTP/2)
pub fn build_icmp_port_unreachable(orig_pkt: &[u8]) -> Option<Vec<u8>> {
    if orig_pkt.len() < 28 {
        return None;
    }
    match orig_pkt[0] >> 4 {
        4 => {
            // IPv4: IP header (20B) + ICMP header (8B) + original IP header (20B) + original 8 bytes payload (8B)
            let orig_ip_header_len = ((orig_pkt[0] & 0x0F) * 4) as usize;
            if orig_pkt.len() < orig_ip_header_len + 8 {
                return None;
            }
            let orig_src = [orig_pkt[12], orig_pkt[13], orig_pkt[14], orig_pkt[15]];
            let orig_dst = [orig_pkt[16], orig_pkt[17], orig_pkt[18], orig_pkt[19]];

            let icmp_payload_len = (orig_ip_header_len + 8).min(orig_pkt.len());
            let total_len = 20 + 8 + icmp_payload_len;
            let mut pkt = vec![0u8; total_len];

            // Outer IPv4 Header
            pkt[0] = 0x45;
            pkt[2..4].copy_from_slice(&(total_len as u16).to_be_bytes());
            pkt[8] = 64; // TTL
            pkt[9] = 1;  // Protocol: ICMP (1)
            pkt[12..16].copy_from_slice(&orig_dst); // Src = original Dst
            pkt[16..20].copy_from_slice(&orig_src); // Dst = original Src
            let ip_csum = checksum(&pkt[0..20]);
            pkt[10..12].copy_from_slice(&ip_csum.to_be_bytes());

            // ICMP Header (Type 3: Destination Unreachable, Code 3: Port Unreachable)
            pkt[20] = 3;
            pkt[21] = 3;
            // 22..24 checksum (computed below)
            // 24..28 unused (0)

            // ICMP Payload: copy original IP header + first 8 bytes of original datagram
            pkt[28..28 + icmp_payload_len].copy_from_slice(&orig_pkt[..icmp_payload_len]);

            let icmp_csum = checksum(&pkt[20..]);
            pkt[22..24].copy_from_slice(&icmp_csum.to_be_bytes());

            Some(pkt)
        }
        _ => None,
    }
}

// ── 入站 UDP 数据报解析 (泵线程调用) ────────────────────────────────────────

/// 解析一个 TUN 数据报的 (src, dst, payload); 非 UDP 返回 None。
pub fn parse_udp_datagram(pkt: &[u8]) -> Option<(SocketAddr, SocketAddr, &[u8])> {
    if pkt.is_empty() {
        return None;
    }
    match pkt[0] >> 4 {
        4 => {
            let ip = Ipv4Packet::new_checked(pkt).ok()?;
            if ip.next_header() != IpProtocol::Udp {
                return None;
            }
            let udp = UdpPacket::new_checked(ip.payload()).ok()?;
            let src: Ipv4Addr = ip.src_addr().into();
            let src = SocketAddr::new(IpAddr::V4(src), udp.src_port());
            let dst: Ipv4Addr = ip.dst_addr().into();
            let dst = SocketAddr::new(IpAddr::V4(dst), udp.dst_port());
            Some((src, dst, udp.payload()))
        }
        6 => {
            let ip = Ipv6Packet::new_checked(pkt).ok()?;
            if ip.next_header() != IpProtocol::Udp {
                return None;
            }
            let udp = UdpPacket::new_checked(ip.payload()).ok()?;
            let src: Ipv6Addr = ip.src_addr().into();
            let src = SocketAddr::new(IpAddr::V6(src), udp.src_port());
            let dst: Ipv6Addr = ip.dst_addr().into();
            let dst = SocketAddr::new(IpAddr::V6(dst), udp.dst_port());
            Some((src, dst, udp.payload()))
        }
        _ => None,
    }
}

#[allow(dead_code)]
fn _mtu_const() -> usize {
    TUN_MTU
}

/// 防未使用告警: Ipv4Addr/Ipv6Addr 在测试/未来功能里用。
#[allow(dead_code)]
fn _type_pins() -> (Ipv4Addr, Ipv6Addr, Duration) {
    (Ipv4Addr::UNSPECIFIED, Ipv6Addr::UNSPECIFIED, UDP_IDLE)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造一个标准 DNS 查询 IP 包 (IPv4, 从 10.99.0.2:53421 到 198.18.0.1:53)
    fn dns_query_pkt() -> Vec<u8> {
        // DNS payload: ID=0x1234, flags=0x0100(RD), 1 question, qname www.google.com, type A, class IN
        let mut q = vec![0x12, 0x34, 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0];
        for label in ["www", "google", "com"] {
            q.push(label.len() as u8);
            q.extend_from_slice(label.as_bytes());
        }
        q.extend_from_slice(&[0, 0, 1, 0, 1]); // root + A + IN
        // IP 头 + UDP 头
        let udp_len = 8 + q.len();
        let total = 20 + udp_len;
        let mut p = vec![0u8; total];
        p[0] = 0x45;
        p[2..4].copy_from_slice(&(total as u16).to_be_bytes());
        p[8] = 64; p[9] = 17;
        p[12..16].copy_from_slice(&[10, 99, 0, 2]);
        p[16..20].copy_from_slice(&[198, 18, 0, 1]);
        let u = &mut p[20..];
        u[0..2].copy_from_slice(&53421u16.to_be_bytes());
        u[2..4].copy_from_slice(&53u16.to_be_bytes());
        u[4..6].copy_from_slice(&(udp_len as u16).to_be_bytes());
        u[8..].copy_from_slice(&q);
        p
    }

    #[test]
    fn parses_udp_datagram() {
        let pkt = dns_query_pkt();
        let parsed = parse_udp_datagram(&pkt);
        assert!(parsed.is_some(), "应能解析出 UDP 数据报");
        let (src, dst, payload) = parsed.unwrap();
        assert_eq!(src, "10.99.0.2:53421".parse::<SocketAddr>().unwrap());
        assert_eq!(dst, "198.18.0.1:53".parse::<SocketAddr>().unwrap());
        assert!(payload.len() > 12);
    }

    #[test]
    fn reply_ip_checksums() {
        // 回程包校验和必须能通过 smoltcp 解析 (不报错)
        let pkt = dns_query_pkt();
        let (src, dst, payload) = parse_udp_datagram(&pkt).unwrap();
        // 回程: from dst to src
        let reply = build_reply_ip_public(dst, src, payload).unwrap();
        let ip = smoltcp::wire::Ipv4Packet::new_checked(&reply);
        assert!(ip.is_ok(), "回程 IP 包应合法: {ip:?}");
        // UDP 校验和由 smoltcp 的 UdpPacket::new_checked 校验 (需要 checksum feature)
        let udp = smoltcp::wire::UdpPacket::new_checked(ip.unwrap().payload());
        assert!(udp.is_ok(), "回程 UDP 校验和应合法: {udp:?}");
    }
}

#[test]
fn reply_ip_header_direction() {
    // 回归: IP 头 12-15 = 源, 16-19 = 目的 (早前写反导致回程包地址颠倒)
    let src: std::net::SocketAddrV4 = "198.19.0.53:53".parse().unwrap();
    let dst: std::net::SocketAddrV4 = "198.18.0.1:39261".parse().unwrap();
    let pkt = build_ipv4_udp(src, dst, b"x");
    assert_eq!(&pkt[12..16], &[198, 19, 0, 53], "12-15 应是源地址");
    assert_eq!(&pkt[16..20], &[198, 18, 0, 1], "16-19 应是目的地址");
    assert_eq!(&pkt[20..22], &[0, 53], "UDP 源端口");
    assert_eq!(&pkt[22..24], &[153, 93], "UDP 目的端口 (39261)");
}

/// 直连 UDP 流: protect UDP socket 直接收发 (回程构 IP 包写 TUN)。
async fn udp_flow_direct(
    stack: Arc<TunStack>,
    _engine: Arc<Engine>,
    key: FlowKey,
    mut rx: tokio::sync::mpsc::Receiver<(SocketAddr, SocketAddr, Vec<u8>)>,
) {
    let sock = match tokio::net::UdpSocket::bind("0.0.0.0:0").await {
        Ok(s) => s,
        Err(_) => return,
    };
    // protect: 绕过 TUN (否则 UDP 又被卷回)
    crate::protect::protect(std::os::unix::io::AsRawFd::as_raw_fd(&sock));

    let dst = SocketAddr::new(key.dst, key.dst_port);
    let client = SocketAddr::new(key.src, key.src_port);
    debug!("[TUN-UDP/direct] 新流 {} → {}", fmt_flow(&key), dst);

    let (cid, conn_up, conn_down) = crate::monitor::record_conn_start("UDP", &format!("{}:{}", key.dst, key.dst_port), "直连");
    let sock_rc = std::sync::Arc::new(sock);

    // 下行: 客户端 → 目标
    let dn_sock = sock_rc.clone();
    let dn_dst = dst;
    let up_atomic = conn_up.clone();
    let dn = async move {
        let mut n_sent: u64 = 0;
        loop {
            let (_, _dst, payload) = match tokio::time::timeout(UDP_IDLE, rx.recv()).await {
                Ok(Some(v)) => v,
                _ => break,
            };
            if dn_sock.send_to(&payload, dn_dst).await.is_err() {
                break;
            }
            let plen = payload.len() as u64;
            n_sent += plen;
            up_atomic.fetch_add(plen, Ordering::Relaxed);
            crate::monitor::add_up(plen);
        }
        n_sent
    };

    // 上行: 目标 → 客户端 (构回程 IP 包)
    let up_sock = sock_rc.clone();
    let up_client = client;
    let up_dst = dst;
    let down_atomic = conn_down.clone();
    let up = async move {
        let mut n_recv: u64 = 0;
        let mut buf = vec![0u8; 65536];
        loop {
            let (n, _from) = match tokio::time::timeout(UDP_IDLE, up_sock.recv_from(&mut buf)).await {
                Ok(Ok(v)) => v,
                _ => break,
            };
            // 回程: src = 原目标, dst = 客户端
            if let Some(pkt) = build_reply_ip_public(up_dst, up_client, &buf[..n]) {
                stack.write_raw(&pkt);
            }
            let n_u64 = n as u64;
            n_recv += n_u64;
            down_atomic.fetch_add(n_u64, Ordering::Relaxed);
            crate::monitor::add_down(n_u64);
        }
        n_recv
    };

    let (sent, recv) = tokio::join!(dn, up);
    crate::monitor::record_conn_close(cid, sent, recv);
    debug!("[TUN-UDP/direct] {} 关闭 (↑{} ↓{})", fmt_flow(&key),
        human_bytes(sent), human_bytes(recv));
}
