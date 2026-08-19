//! UDP 多路复用 (session-id over shared Mirage tunnel)。
//!
//! 帧格式 (双向, 紧接 [0x01] mux sentinel 之后):
//!   [2B frameLen][4B sid][1B ATYP][ADDR][2B port][payload]
//! frameLen = 2B 长度字段之后的字节数 = 4 + 1 + addrlen + 2 + payloadlen。
//! ATYP: 1=IPv4, 3=Domain, 4=IPv6 (与 legacy udp_relay 帧一致, 仅在 frameLen 后插 4B sid)。
//! sid: u32, 客户端在一条共享隧道内唯一分配。上行=目标地址; 下行=回包源地址。

use std::collections::HashMap;
use std::net::{Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, LazyLock, Mutex as StdMutex};
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::timeout;

use crate::proxy::pool::WarmPool;
use crate::tun::udp::FlowKey;

/// mux 模式 sentinel: 客户端在共享隧道上首个 send_data 发此单字节。
/// 服务端 control 分派: len==1 && [0]==0x01 → mux relay。
pub const MUX_SENTINEL: u8 = 0x01;

// ── 全局门控 ──────────────────────────────────────────────────────────────
static UDP_MUX_ENABLED: AtomicBool = AtomicBool::new(true);
static UDP_MUX_TUNNELS: AtomicUsize = AtomicUsize::new(4);

pub fn set_udp_mux(enabled: bool, tunnels: usize) {
    UDP_MUX_ENABLED.store(enabled, Ordering::Relaxed);
    UDP_MUX_TUNNELS.store(tunnels.max(1), Ordering::Relaxed);
}

pub fn udp_mux_enabled() -> bool {
    UDP_MUX_ENABLED.load(Ordering::Relaxed)
}

pub fn udp_mux_tunnels() -> usize {
    UDP_MUX_TUNNELS.load(Ordering::Relaxed)
}

/// 帧体在 sid 之后、ATYP 之前的固定前缀: 4B sid。
/// body_len 上限 = u16::MAX; 超出封帧返回 None。
fn wrap_frame(sid: u32, atyp_and_addr: &[u8], port: u16, payload: &[u8]) -> Option<Vec<u8>> {
    let body_len = 4 + atyp_and_addr.len() + 2 + payload.len();
    if body_len > u16::MAX as usize {
        return None;
    }
    let mut f = Vec::with_capacity(2 + body_len);
    f.extend_from_slice(&(body_len as u16).to_be_bytes());
    f.extend_from_slice(&sid.to_be_bytes());
    f.extend_from_slice(atyp_and_addr);
    f.extend_from_slice(&port.to_be_bytes());
    f.extend_from_slice(payload);
    Some(f)
}

/// 封上行 mux 帧 (域名目标)。
pub fn frame_mux_domain(sid: u32, domain: &str, port: u16, payload: &[u8]) -> Option<Vec<u8>> {
    if domain.len() > 255 {
        return None;
    }
    let mut a = Vec::with_capacity(2 + domain.len());
    a.push(0x03);
    a.push(domain.len() as u8);
    a.extend_from_slice(domain.as_bytes());
    wrap_frame(sid, &a, port, payload)
}

/// 封上行 mux 帧 (裸 IPv4 目标)。
pub fn frame_mux_ipv4(sid: u32, ip: &Ipv4Addr, port: u16, payload: &[u8]) -> Option<Vec<u8>> {
    let mut a = Vec::with_capacity(5);
    a.push(0x01);
    a.extend_from_slice(&ip.octets());
    wrap_frame(sid, &a, port, payload)
}

/// 封上行 mux 帧 (裸 IPv6 目标)。
pub fn frame_mux_ipv6(sid: u32, ip: &Ipv6Addr, port: u16, payload: &[u8]) -> Option<Vec<u8>> {
    let mut a = Vec::with_capacity(17);
    a.push(0x04);
    a.extend_from_slice(&ip.octets());
    wrap_frame(sid, &a, port, payload)
}

/// 从累积 buffer 解一帧 mux, 返回 (sid, payload, 消费字节数)。不完整返回 None。
pub fn parse_mux_frame(buf: &[u8]) -> Option<(u32, Vec<u8>, usize)> {
    if buf.len() < 2 {
        return None;
    }
    let flen = u16::from_be_bytes([buf[0], buf[1]]) as usize;
    let total = 2 + flen;
    if buf.len() < total {
        return None; // 帧未收全
    }
    let frame = &buf[2..total];
    // 帧太短放不下 4B sid → 消费整帧重同步
    if frame.len() < 4 {
        return Some((0, Vec::new(), total));
    }
    let sid = u32::from_be_bytes([frame[0], frame[1], frame[2], frame[3]]);
    let empty = (sid, Vec::new(), total);
    let inner = &frame[4..]; // ATYP..
    if inner.is_empty() {
        return Some(empty);
    }
    let mut off = 1usize; // 跳 ATYP
    match inner[0] {
        1 => off += 4,
        4 => off += 16,
        3 => {
            if inner.len() < off + 1 {
                return Some(empty);
            }
            let dl = inner[off] as usize;
            off += 1 + dl;
        }
        _ => return Some(empty),
    }
    off += 2; // port
    if off > inner.len() {
        return Some(empty);
    }
    Some((sid, inner[off..].to_vec(), total))
}

// ── 客户端: K 条共享 mux 隧道 ────────────────────────────────────────────────

const MUX_IDLE_TIMEOUT: Duration = Duration::from_secs(60);
/// 共享 writer 泵一次 send_data 前榨干队列的合帧上限 (跨流合并, 抗长度指纹, 零新增延迟)。
const MUX_UPLINK_COALESCE_CAP: usize = 16 * 1024;

/// 单条共享隧道内的 sid 上限, 封顶单隧道资源。
const MUX_MAX_SIDS_PER_TUNNEL: usize = 512;
/// 首个下行到达前的快拆超时。
pub const MUX_FIRST_DOWNLINK: Duration = Duration::from_secs(8);

pub struct FlowEntry {
    pub stack: Arc<crate::tun::TunStack>,
    pub key: FlowKey,
    pub got_downlink: Arc<AtomicBool>,
    pub down_counter: Option<Arc<AtomicU64>>,
}

/// 客户端一条共享 mux 隧道。多条 UDP 流按 sid 复用:
/// 上行经共享 writer 泵合帧发出, 下行由单一 demux 泵按 sid 路由并直接构造回程 IP 包写入 TUN。
pub struct MuxTunnel {
    uplink_tx: mpsc::Sender<Vec<u8>>, // 预封帧字节 → 共享 writer 泵
    flows: Arc<StdMutex<HashMap<u32, FlowEntry>>>,
    next_sid: AtomicU32,
    alive: Arc<AtomicBool>,
}

impl MuxTunnel {
    /// 从 pool 取一条隧道, 发 MUX_SENTINEL, spawn 共享 writer + demux 泵。
    async fn create(pool: &Arc<WarmPool>) -> anyhow::Result<Arc<MuxTunnel>> {
        let mut tunnel = pool.get().await?;
        tunnel.writer.send_data(&[MUX_SENTINEL]).await?;
        Ok(Self::from_tunnel(tunnel))
    }

    /// 用一条已就绪 (sentinel 已发) 的隧道 spawn 共享 writer + demux 泵。
    fn from_tunnel(tunnel: crate::proxy::tunnel::Tunnel) -> Arc<MuxTunnel> {
        let (uplink_tx, mut uplink_rx) = mpsc::channel::<Vec<u8>>(1024);
        let flows: Arc<StdMutex<HashMap<u32, FlowEntry>>> = Default::default();
        let alive = Arc::new(AtomicBool::new(true));

        // writer 泵: 唯一 AEAD 写点。合帧后 send_data。
        let mut writer = tunnel.writer;
        let alive_w = alive.clone();
        tokio::spawn(async move {
            while let Some(first) = uplink_rx.recv().await {
                let mut batch = first;
                while batch.len() < MUX_UPLINK_COALESCE_CAP {
                    match uplink_rx.try_recv() {
                        Ok(b) => batch.extend_from_slice(&b),
                        Err(_) => break,
                    }
                }
                if writer.send_data(&batch).await.is_err() {
                    break;
                }
            }
            alive_w.store(false, Ordering::Relaxed);
        });

        // demux 泵: read → parse_mux_frame → 按 sid 找 flow → 构造回程 IP 包写 TUN。
        let mut reader = tunnel.reader;
        let flows_d = flows.clone();
        let alive_d = alive.clone();
        tokio::spawn(async move {
            let mut acc: Vec<u8> = Vec::new();
            loop {
                let chunk = match timeout(MUX_IDLE_TIMEOUT, reader.recv_data()).await {
                    Ok(Ok(c)) => c,
                    _ => break,
                };
                acc.extend_from_slice(&chunk);
                while let Some((sid, payload, consumed)) = parse_mux_frame(&acc) {
                    if !payload.is_empty() {
                        let entry = flows_d
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .get(&sid)
                            .map(|e| {
                                (
                                    e.stack.clone(),
                                    e.key,
                                    e.got_downlink.clone(),
                                    e.down_counter.clone(),
                                )
                            });
                        if let Some((stack, key, got, down_counter)) = entry {
                            got.store(true, Ordering::Relaxed);
                            let reply_src = SocketAddr::new(key.dst, key.dst_port);
                            let reply_dst = SocketAddr::new(key.src, key.src_port);
                            if let Some(pkt) = crate::tun::udp::build_reply_ip_public(
                                reply_src, reply_dst, &payload,
                            ) {
                                stack.write_raw(&pkt);
                            }
                            if let Some(dc) = down_counter {
                                dc.fetch_add(payload.len() as u64, Ordering::Relaxed);
                            }
                        }
                    }
                    acc.drain(0..consumed);
                }
                if acc.len() > 65536 * 2 {
                    break;
                }
            }
            alive_d.store(false, Ordering::Relaxed);
        });

        Arc::new(MuxTunnel {
            uplink_tx,
            flows,
            next_sid: AtomicU32::new(1),
            alive,
        })
    }

    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Relaxed)
    }

    pub fn alloc_sid(&self) -> u32 {
        self.next_sid.fetch_add(1, Ordering::Relaxed)
    }

    fn unregister(&self, sid: u32) {
        self.flows
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&sid);
    }

    /// 注册 sid → (stack, key, got_downlink, down_counter)。返回 (RAII 守卫, got_downlink 标志)。
    pub fn try_register(
        self: &Arc<Self>,
        sid: u32,
        stack: Arc<crate::tun::TunStack>,
        key: FlowKey,
        down_counter: Option<Arc<AtomicU64>>,
    ) -> Option<(SidGuard, Arc<AtomicBool>)> {
        let got_downlink = Arc::new(AtomicBool::new(false));
        let mut flows = self.flows.lock().unwrap_or_else(|e| e.into_inner());
        if flows.len() >= MUX_MAX_SIDS_PER_TUNNEL {
            return None;
        }
        flows.insert(
            sid,
            FlowEntry {
                stack,
                key,
                got_downlink: got_downlink.clone(),
                down_counter,
            },
        );
        Some((
            SidGuard {
                mtun: self.clone(),
                sid,
            },
            got_downlink,
        ))
    }

    /// 共享上行发送端。
    pub fn uplink(&self) -> mpsc::Sender<Vec<u8>> {
        self.uplink_tx.clone()
    }
}

/// sid 注册的 RAII 守卫: drop 即从 MuxTunnel.flows 注销该 sid。
pub struct SidGuard {
    mtun: Arc<MuxTunnel>,
    sid: u32,
}

impl Drop for SidGuard {
    fn drop(&mut self) {
        self.mtun.unregister(self.sid);
    }
}

/// flowkey → slot 索引 (恒在 `[0, num_slots)`)。
pub(crate) fn slot_index(key: &FlowKey, num_slots: usize) -> usize {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    key.hash(&mut h);
    (h.finish() as usize) % num_slots.max(1)
}

/// K 条共享 mux 隧道。按 flowkey 散列选隧道 (HoL 分摊); 懒创建; 死则重建。
pub struct MuxSet {
    pool: std::sync::Weak<WarmPool>,
    slots: Vec<tokio::sync::Mutex<Option<Arc<MuxTunnel>>>>,
}

impl MuxSet {
    fn new(pool: &Arc<WarmPool>, k: usize) -> Self {
        let k = k.max(1);
        MuxSet {
            pool: Arc::downgrade(pool),
            slots: (0..k).map(|_| tokio::sync::Mutex::new(None)).collect(),
        }
    }

    fn slot_for(&self, key: &FlowKey) -> usize {
        slot_index(key, self.slots.len())
    }

    async fn get(&self, key: &FlowKey) -> anyhow::Result<Arc<MuxTunnel>> {
        let pool = self
            .pool
            .upgrade()
            .ok_or_else(|| anyhow::anyhow!("mux: 底层 pool 已释放 (配置重载?)"))?;
        let idx = self.slot_for(key);
        let mut slot = self.slots[idx].lock().await;
        if let Some(t) = slot.as_ref() {
            if t.is_alive() {
                return Ok(t.clone());
            }
        }
        let t = MuxTunnel::create(&pool).await?;
        *slot = Some(t.clone());
        Ok(t)
    }
}

/// pool 指针 → 该 pool 的 MuxSet (每 Mirage outbound 一套共享隧道)。
static REGISTRY: LazyLock<StdMutex<HashMap<usize, Arc<MuxSet>>>> =
    LazyLock::new(|| StdMutex::new(HashMap::new()));

/// 取 (或懒建/重建) 给定 pool + flowkey 对应的共享 mux 隧道。
pub async fn get_mux_tunnel(
    pool: &Arc<WarmPool>,
    key: &FlowKey,
) -> anyhow::Result<Arc<MuxTunnel>> {
    let ptr = Arc::as_ptr(pool) as usize;
    let set = {
        let mut reg = REGISTRY.lock().unwrap_or_else(|e| e.into_inner());
        reg.retain(|_, set| set.pool.strong_count() > 0);
        reg.entry(ptr)
            .or_insert_with(|| Arc::new(MuxSet::new(pool, udp_mux_tunnels())))
            .clone()
    };
    set.get(key).await
}
