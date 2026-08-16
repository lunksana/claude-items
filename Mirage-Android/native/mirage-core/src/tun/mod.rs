//! TUN 引擎: TUN fd → smoltcp 用户态协议栈 → Mirage 隧道。
//!
//! ```text
//! App (VpnService / NEPacketTunnelProvider 提供 TUN fd)
//!   │  dup(fd)
//!   ▼
//! [读线程]  read(TUN fd) ──包──▶ 泵任务 (Interface::poll)
//!                                  │ pre-scan: SYN→建 catcher socket / UDP→建 bound socket
//!                                  │ 数据面: smoltcp socket ⇄ Mirage 隧道 (WarmPool)
//!   ◀──包──── 泵任务 drain tx ── write(TUN fd)
//! ```
//!
//! ## 为什么 TUN 连接要"惰性建 socket"
//!
//! 移动端 TUN 是**透明代理** (包的目的地址是真实目标, 不是本机地址), 而 smoltcp 是
//! 主机栈 (只接受发给自己的连接)。解法 (与 meow/netstack-smoltcp 同思路, 零改动 vanilla
//! smoltcp):
//!
//! - **TCP**: 收到 SYN 时预扫描包头, 若 4 元组无匹配 socket, 建一个 `listen(None, dst_port)`
//!   catcher —— smoltcp 的 `ListenEndpoint.addr=None` 会接受发往**任意地址**该端口的 SYN,
//!   于是这个 SYN 被接住, socket 随即进入 Established (4 元组绑定)。每个新 4 元组一条 socket,
//!   天然支持并发 (每个 catcher 只被一条连接消费)。
//! - **UDP**: 收到数据报时若 `(dst_addr, dst_port)` 无匹配 socket, 建一个 `bind(dst_addr, dst_port)`
//!   的 socket —— smoltcp 会把发往该精确目的的数据报都投给它。
//! - **DNS**: `(dns_addr, 53)` 的 TCP/UDP 走本地 fake-IP DNS 应答器, 不代理。
//!
//! 建完 socket 立刻 spawn 对应 relay 任务 (TCP: 等 Established → 双向转发; UDP: 封装帧 →
//! 隧道), 无泵任务轮询扫描, 避免 SocketHandle 复用竞态。
//!
//! ## 平台无关
//! 本模块只依赖一个 `RawFd` (TUN 接口), Android/iOS 皆可用; 上层包装见
//! `mirage-jni` (Android) 与规划中的 `mirage-ios`。

pub mod dns;
pub mod device;
pub mod tcp;
pub mod udp;

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::os::fd::RawFd;
use std::os::unix::io::FromRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};

use smoltcp::iface::{Config as IfConfig, Interface, SocketHandle, SocketSet};
use smoltcp::socket::tcp as stcp;
use smoltcp::time::Instant as SmolInstant;
use smoltcp::wire::{HardwareAddress, IpAddress, IpCidr, Ipv4Address, IpProtocol};
use tokio::sync::Notify;
use tracing::{debug, info};

use crate::engine::{Engine, TUN_ADDR_V4, TUN_DNS_V4, TUN_PEER_V4};
use crate::tun::device::TunDevice;

pub const TUN_MTU: usize = 1500;
/// TCP socket 收发缓冲 (每条连接 2×64KB)。与上游 WG 模块对齐。
pub const SOCK_BUF: usize = 64 * 1024;
/// 无 4 元组的 catcher socket (SYN 来了没人连) 的存活上限, 到点由 sweeper 回收。
const CATCHER_TTL: Duration = Duration::from_secs(30);
/// 泵的定时 tick (驱动 smoltcp 计时器: 重传/窗口/超时)。
const PUMP_TICK: Duration = Duration::from_millis(25);

pub struct TunConfig {
    pub mtu: usize,
    pub local_addr: Ipv4Address,
    pub dns_addr: Ipv4Address,
    pub peer_addr: Ipv4Address,
}

impl Default for TunConfig {
    fn default() -> Self {
        Self {
            mtu: TUN_MTU,
            local_addr: TUN_ADDR_V4.parse().unwrap(),
            dns_addr: TUN_DNS_V4.parse().unwrap(),
            peer_addr: TUN_PEER_V4.parse().unwrap(),
        }
    }
}

/// smoltcp 栈 + socket 集。所有 socket 访问都在这把锁下 (**不得在持锁时 await**,
/// 与上游 WG TunnelInner 同一约束)。
struct TunInner {
    iface: Interface,
    device: TunDevice,
    sockets: SocketSet<'static>,
    /// catcher/bound socket 的创建时刻 (sweeper 判断 stale 用)。
    created_at: HashMap<SocketHandle, Instant>,
}

/// TUN 引擎。`start` 后持有读线程 JoinHandle 与泵任务句柄。
pub struct TunStack {
    inner: Arc<StdMutex<TunInner>>,
    /// 引擎引用, 支持**运行时热切换** (节点切换: setNode 时替换整个 Engine)。
    engine: Arc<arc_swap::ArcSwap<Engine>>,
    wake: Arc<Notify>,
    /// 已停止标志 (stop 幂等)。
    stopped: Arc<AtomicBool>,
    /// 泵的退出信号 (stop 时 notify)。
    stop_notify: Arc<Notify>,
    cfg: TunConfig,
    /// 原始 TUN fd (stop 时关闭; 读线程持 dup 副本)。
    fd: RawFd,
    /// 写 TUN fd 的 File (drain_tx / write_raw 用)。
    write_file: StdMutex<std::fs::File>,
    /// UDP 直接数据报引擎 (按 (client,dst) 建流)。
    udp: Arc<crate::tun::udp::UdpEngine>,
}

fn lock_inner(s: &Arc<StdMutex<TunInner>>) -> std::sync::MutexGuard<'_, TunInner> {
    s.lock().unwrap_or_else(|e| e.into_inner())
}

impl TunStack {
    pub fn engine(&self) -> Arc<Engine> {
        self.engine.load_full()
    }

    /// 运行时热切换引擎 (节点切换): 替换引用, 后续连接走新节点。
    /// 现有隧道保留到自然断开; 新连接/新流用新引擎。
    pub fn swap_engine(&self, new: Arc<Engine>) {
        self.engine.store(new);
        tracing::info!("[TUN] 引擎已热切换 (节点/规则更新)");
    }

    /// 启动 TUN 引擎: dup fd → 读线程 + 泵任务。返回后引擎即在工作。
    pub async fn start(engine: Arc<Engine>, cfg: TunConfig, tun_fd: RawFd) -> std::io::Result<Arc<Self>> {
        let fd = unsafe { libc::dup(tun_fd) };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // 非阻塞读: 泵 select 用不到, 但避免读线程死等。
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
        };

        let if_cfg = IfConfig::new(HardwareAddress::Ip);
        let now = std::time::Instant::now();
        let mut device = TunDevice::new(cfg.mtu);
        let mut iface = Interface::new(if_cfg, &mut device, smol_now(now));
        iface.update_ip_addrs(|addrs| {
            let _ = addrs.push(IpCidr::new(IpAddress::Ipv4(cfg.local_addr), 32));
        });
        iface.routes_mut().add_default_ipv4_route(cfg.peer_addr).ok();
        // 透明代理关键开关: 接受发往**任意地址**的包 (TUN 里目的地址是真实目标, 非本机
        // 地址), 并允许以任意源地址发出应答 (SYN-ACK 源 = 客户端连接的目的地址)。
        // 没有它, smoltcp 只认自己接口地址的包, SYN 进不来、SYN-ACK 也发不出。
        iface.set_any_ip(true);
        // 假 IP 段路由也收进来 (App 端 addRoute("198.18.0.0", 15) 后, 目的为 fake-IP
        // 的包同样要过 smoltcp; 默认路由已覆盖, 这里显式加是为了语义清晰)。
        // (smoltcp 路由表只有一条默认路由, 已足够。)

        let inner = Arc::new(StdMutex::new(TunInner {
            iface,
            device,
            sockets: SocketSet::new(Vec::new()),
            created_at: HashMap::new(),
        }));

        // 写 TUN 用的 File (读线程单独 dup, 互不干扰)
        let write_file = StdMutex::new(unsafe { std::fs::File::from_raw_fd(libc::dup(fd)) });

        let mtu = cfg.mtu;
        let stack = Arc::new(Self {
            inner,
            udp: Arc::new(crate::tun::udp::UdpEngine::new(Arc::clone(&engine))),
            wake: Arc::new(Notify::new()),
            stopped: Arc::new(AtomicBool::new(false)),
            stop_notify: Arc::new(Notify::new()),
            engine: Arc::new(arc_swap::ArcSwap::from(Arc::clone(&engine))),
            cfg,
            fd,
            write_file,
        });

        // 读线程: TUN fd → 包 → 泵 (持 dup 副本; O_NONBLOCK + 短眠轮询, stop 时退出)
        let reader_stack = stack.clone();
        let (tx, rx) = tokio::sync::mpsc::channel::<Vec<u8>>(1024);
        std::thread::spawn(move || {
            let rfd = unsafe { libc::dup(reader_stack.fd) };
            if rfd < 0 {
                return;
            }
            let mut f = unsafe { std::fs::File::from_raw_fd(rfd) };
            let mut buf = vec![0u8; 65536];
            loop {
                if reader_stack.stopped.load(Ordering::SeqCst) {
                    break;
                }
                match std::io::Read::read(&mut f, &mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        if tx.blocking_send(buf[..n].to_vec()).is_err() {
                            break; // 泵已退出
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
            let _ = reader_stack.engine.clone(); // keep alive until thread exit
        });

        // 泵任务
        let pump_stack = stack.clone();
        tokio::spawn(pump(pump_stack, rx));

        info!("TUN 引擎已启动 (mtu={}, fd={})", mtu, fd);
        Ok(stack)
    }

    /// 停止引擎 (关闭 fd, 泵退出)。幂等。
    pub fn stop(&self) {
        if self.stopped.swap(true, Ordering::SeqCst) {
            return;
        }
        unsafe { libc::close(self.fd) };
        self.wake.notify_waiters();
        self.stop_notify.notify_waiters();
    }

    /// 驱动一次 smoltcp poll (同步, 从 socket 访问方调用, 如 TCP relay 写完后)。
    /// 用完必须 notify 泵去 drain tx + 写回 TUN。
    pub fn poll_now(&self) {
        {
            let mut g = lock_inner(&self.inner);
            let TunInner { iface, device, sockets, .. } = &mut *g;
            iface.poll(smol_now(Instant::now()), device, sockets);
        }
        self.wake.notify_one();
    }

    /// (仅测试) 判断 socket 是否 Established。
    #[cfg(test)]
    pub(crate) fn is_established(&self, handle: SocketHandle) -> bool {
        let g = lock_inner(&self.inner);
        g.sockets
            .get::<stcp::Socket>(handle)
            .state()
            == stcp::State::Established
    }
}

/// 泵: rx 包 / 定时 tick / 外部唤醒 → pre-scan + poll → drain tx 写回 TUN。
async fn pump(stack: Arc<TunStack>, mut rx: tokio::sync::mpsc::Receiver<Vec<u8>>) {
    let mut tick = tokio::time::interval(PUMP_TICK);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = stack.stop_notify.notified() => break,
            maybe_pkt = rx.recv() => {
                match maybe_pkt {
                    Some(pkt) => {
                        stack.handle_rx_packet(pkt);
                        stack.drain_tx();
                        stack.sweep();
                    }
                    None => break, // 读线程退出 (fd 关闭)
                }
            }
            _ = tick.tick() => {
                stack.drain_tx();
                stack.sweep();
            }
            _ = stack.wake.notified() => {
                stack.drain_tx();
            }
        }
    }
    // 泵退出: 关闭 fd 副本 (读线程会因读失败退出)
    stack.stop();
    info!("TUN 泵已退出");
}

impl TunStack {
    /// 处理一个入站 IP 包: UDP 走直接数据报路径 (绕开 smoltcp, 见 udp.rs);
    /// 其余 (TCP/ICMP…) 预扫描建 socket → 入设备队列 → poll。
    fn handle_rx_packet(self: &Arc<Self>, pkt: Vec<u8>) {
        // UDP: 直接数据报路径 (按 (client,dst) 建流, 回程伪源构包)
        if let Some((src, dst, payload)) = crate::tun::udp::parse_udp_datagram(&pkt) {
            if dst == SocketAddr::new(IpAddr::V4(self.cfg.dns_addr), 53) {
                // DNS 分流 (国内→真实 IP 直连 / 国外→fake-IP 代理)
                crate::tun::dns::handle_dns_query(Arc::clone(self), src, &payload);
                return;
            }
            self.udp.feed(Arc::clone(self), src, dst, payload);
            return;
        }

        // TCP / 其他 → smoltcp
        self.prescan(&pkt);

        let mut g = lock_inner(&self.inner);
        let TunInner { iface, device, sockets, .. } = &mut *g;
        device.push_rx(pkt);
        iface.poll(smol_now(Instant::now()), device, sockets);
    }

    /// DNS 地址 (std 类型, 供应答构包)。
    pub fn dns_addr_std(&self) -> std::net::Ipv4Addr {
        self.cfg.dns_addr.into()
    }

    /// 直接把一个 IP 包写回 TUN fd (UDP 回程/DNS 应答用)。线程安全。
    pub fn write_raw(&self, pkt: &[u8]) {
        if self.stopped.load(Ordering::SeqCst) {
            return;
        }
        let mut f = self.write_file.lock().unwrap_or_else(|e| e.into_inner());
        let _ = std::io::Write::write_all(&mut *f, pkt);
    }

    /// 把 smoltcp 产出的出站包全部写回 TUN fd。**锁外执行**。
    fn drain_tx(&self) {
        if self.stopped.load(Ordering::SeqCst) {
            return;
        }
        let mut out = Vec::new();
        {
            let mut g = lock_inner(&self.inner);
            while let Some(p) = g.device.pop_tx() {
                out.push(p);
            }
        }
        if out.is_empty() {
            return;
        }
        let mut f = self.write_file.lock().unwrap_or_else(|e| e.into_inner());
        for p in &out {
            let _ = std::io::Write::write_all(&mut *f, p);
        }
    }

    /// 回收过期的无 4 元组 catcher socket (SYN 来了没人连)。
    fn sweep(&self) {
        let mut g = lock_inner(&self.inner);
        let now = Instant::now();
        let stale: Vec<SocketHandle> = g
            .sockets
            .iter()
            .filter(|(h, s)| {
                let age = g.created_at.get(h).copied().unwrap_or(now);
                if now.duration_since(age) < CATCHER_TTL {
                    return false;
                }
                match s {
                    smoltcp::socket::Socket::Tcp(t) => {
                        t.state() == stcp::State::Listen && t.remote_endpoint().is_none()
                    }
                    _ => false,
                }
            })
            .map(|(h, _)| h)
            .collect();
        if !stale.is_empty() {
            debug!("[TUN] 回收 {} 个过期 socket", stale.len());
        }
        for h in stale {
            g.created_at.remove(&h);
            g.sockets.remove(h);
        }
    }

    /// 预扫描入站 IP 包, 为新的 TCP 连接 / UDP 目的建 smoltcp socket 并 spawn relay。
    fn prescan(self: &Arc<Self>, pkt: &[u8]) {
        if pkt.is_empty() {
            return;
        }
        match pkt[0] >> 4 {
            4 => self.prescan_ipv4(pkt),
            6 => self.prescan_ipv6(pkt),
            _ => {}
        }
    }

    fn prescan_ipv4(self: &Arc<Self>, pkt: &[u8]) {
        use smoltcp::wire::Ipv4Packet;
        let Ok(ip) = Ipv4Packet::new_checked(pkt) else { return };
        if ip.next_header() == IpProtocol::Tcp {
            let dst = IpAddress::Ipv4(ip.dst_addr());
            let src = IpAddress::Ipv4(ip.src_addr());
            self.prescan_tcp(src, dst, ip.payload());
        }
    }

    fn prescan_ipv6(self: &Arc<Self>, pkt: &[u8]) {
        use smoltcp::wire::Ipv6Packet;
        let Ok(ip) = Ipv6Packet::new_checked(pkt) else { return };
        if ip.next_header() == IpProtocol::Tcp {
            let dst = IpAddress::Ipv6(ip.dst_addr());
            let src = IpAddress::Ipv6(ip.src_addr());
            self.prescan_tcp(src, dst, ip.payload());
        }
    }

    /// TCP: SYN (无 ACK) 且 4 元组无匹配 → 建 catcher + spawn relay。
    fn prescan_tcp(self: &Arc<Self>, src: IpAddress, dst: IpAddress, payload: &[u8]) {
        use smoltcp::wire::TcpPacket;
        let Ok(tcp) = TcpPacket::new_checked(payload) else { return };
        if !tcp.syn() || tcp.ack() {
            return; // 只对全新连接的第一个 SYN 建 socket
        }
        let dst_port = tcp.dst_port();
        let src_port = tcp.src_port();
        if dst_port == 0 || src_port == 0 {
            return;
        }

        let mut g = lock_inner(&self.inner);

        // 4 元组已有 socket (重传/已建连接) → 跳过
        let exists = g.sockets.iter().any(|(_, s)| match s {
            smoltcp::socket::Socket::Tcp(t) => {
                if let (Some(local), Some(remote)) = (t.local_endpoint(), t.remote_endpoint()) {
                    local.addr == dst && local.port == dst_port
                        && remote.addr == src && remote.port == src_port
                } else {
                    false
                }
            }
            _ => false,
        });
        if exists {
            return;
        }

        // 建 catcher
        let sock = stcp::Socket::new(
            stcp::SocketBuffer::new(vec![0u8; SOCK_BUF]),
            stcp::SocketBuffer::new(vec![0u8; SOCK_BUF]),
        );
        let handle = g.sockets.add(sock);
        let g = &mut *g;
        let listen_endpoint = if dst == IpAddress::Ipv4(self.cfg.dns_addr) && dst_port == 53 {
            // DNS over TCP: 精确绑定 DNS 地址
            smoltcp::wire::IpListenEndpoint { addr: Some(dst), port: 53 }
        } else {
            // 通用 catcher: 任意地址的该端口
            smoltcp::wire::IpListenEndpoint { addr: None, port: dst_port }
        };
        if let Err(e) = g.sockets.get_mut::<stcp::Socket>(handle).listen(listen_endpoint) {
            g.sockets.remove(handle);
            debug!("[TUN] catcher listen 失败 ({}:{})", dst_port, e);
            return;
        }
        g.created_at.insert(handle, Instant::now());
        let is_dns = dst == IpAddress::Ipv4(self.cfg.dns_addr) && dst_port == 53;

        // spawn relay (TCP: 等 Established 后转发)
        let stack = self.clone_arc();
        tokio::spawn(async move {
            if is_dns {
                crate::tun::dns::relay_tcp_dns(stack, handle).await;
            } else {
                crate::tun::tcp::relay_tcp(stack, handle).await;
            }
        });
    }

    /// UDP: (dst_addr, dst_port) 无匹配 socket → 建 bound socket + spawn relay。
        fn clone_arc(self: &Arc<Self>) -> Arc<Self> {
        Arc::clone(self)
    }
}

fn smol_now(start: Instant) -> SmolInstant {
    SmolInstant::from_micros(start.elapsed().as_micros() as i64)
}
