//! TCP 数据面: smoltcp TcpSocket → tokio 流适配 + 与 Mirage 隧道的双向中继。
//!
//! 完全复用上游 WG 模块 `WgTcpStream` 的桥接模式:
//! - smoltcp 的**轮询式** socket 用 `register_recv_waker`/`register_send_waker` 接到
//!   tokio waker (需 `async` feature), 无数据时 Pending 而不是 sleep。
//! - 每次 `send_slice`/`recv_slice` 之后必须 `poll_now()` 驱动 smoltcp 生成 IP 包
//!   (否则要等下一个 25ms tick, 每个往返白加延迟)。
//! - socket 关闭必须**先 poll 再 remove**: FIN 是 poll 遍历仍在 SocketSet 里的 socket
//!   时才生成的; remove 在 poll 之后, 否则 FIN 永远发不出去。

use std::io;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll};

use smoltcp::iface::SocketHandle;
use smoltcp::socket::tcp;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};
use tracing::{debug, warn};

use crate::proxy::outbound::Address;
use crate::proxy::tunnel::Tunnel;
use crate::tun::{TunStack, SOCK_BUF, lock_inner};

/// 活跃 TCP 连接计数 (含隧道/直连, 流量监测用)。
pub static TCP_ACTIVE: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// RAII: 退出时 TCP_ACTIVE-1。
struct TcpActiveGuard;
impl Drop for TcpActiveGuard {
    fn drop(&mut self) {
        TCP_ACTIVE.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
    }
}

/// 建连 (SYN→Established) 超时。catcher 被 SYN 接住后一般一个 RTT 内完成握手。
const CONNECT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(15);
/// 隧道 relay 空闲超时 (与上游 handler.rs 的 MIRAGE_RELAY_IDLE 对齐, 双向 1800s 无数据才断)。
const RELAY_IDLE: std::time::Duration = std::time::Duration::from_secs(1800);

/// 一条已建立的 TCP 连接 (smoltcp 侧)。
pub struct TunTcpStream {
    stack: Arc<TunStack>,
    handle: SocketHandle,
    alive: Arc<AtomicBool>,
    /// 缓存的目标 (dst ip, dst port) —— socket 建立后从 local_endpoint 取一次。
    dst: Option<(std::net::IpAddr, u16)>,
}

impl TunTcpStream {
    /// 从 catcher socket 构造; 等到 Established 才返回。
    pub async fn from_catcher(stack: Arc<TunStack>, handle: SocketHandle) -> io::Result<Self> {
        let stream = Self {
            stack,
            handle,
            alive: Arc::new(AtomicBool::new(true)),
            dst: None,
        };
        match tokio::time::timeout(CONNECT_TIMEOUT, stream.wait_established()).await {
            Ok(r) => r?,
            Err(_) => {
                // 超时: stream 在此 drop, Drop 会 close + remove, 不留残骸。
                return Err(io::Error::new(io::ErrorKind::TimedOut, "TCP 握手超时"));
            }
        }
        Ok(stream)
    }

    async fn wait_established(&self) -> io::Result<()> {
        std::future::poll_fn(|cx| {
            let mut g = lock_inner(&self.stack.inner);
            let sock = g.sockets.get_mut::<tcp::Socket>(self.handle);
            match sock.state() {
                tcp::State::Established => Poll::Ready(Ok(())),
                tcp::State::Closed | tcp::State::TimeWait => {
                    Poll::Ready(Err(io::Error::new(io::ErrorKind::ConnectionAborted, "连接被拒绝/重置")))
                }
                _ => {
                    sock.register_recv_waker(cx.waker());
                    sock.register_send_waker(cx.waker());
                    Poll::Pending
                }
            }
        })
        .await
    }

    /// 目标地址 (dst ip:port) —— 连接建立后调用。
    pub fn destination(&mut self) -> Option<(std::net::IpAddr, u16)> {
        if let Some(d) = self.dst {
            return Some(d);
        }
        let g = lock_inner(&self.stack.inner);
        let sock = g.sockets.get::<tcp::Socket>(self.handle);
        let d = sock.local_endpoint().map(|ep| (ep.addr.into(), ep.port));
        drop(g);
        self.dst = d;
        d
    }

    /// 关闭连接并释放 socket (幂等)。
    pub fn close(&self) {
        {
            let mut g = lock_inner(&self.stack.inner);
            if self.alive.load(Ordering::Relaxed) {
                g.sockets.get_mut::<tcp::Socket>(self.handle).close();
            }
        }
        self.stack.poll_now();
        {
            let mut g = lock_inner(&self.stack.inner);
            if self.alive.swap(false, Ordering::Relaxed) {
                g.sockets.remove(self.handle);
                g.created_at.remove(&self.handle);
            }
        }
    }
}

impl Drop for TunTcpStream {
    fn drop(&mut self) {
        self.close();
    }
}

impl AsyncRead for TunTcpStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let mut total = 0;
        let n = {
            let mut g = lock_inner(&self.stack.inner);
            let sock = g.sockets.get_mut::<tcp::Socket>(self.handle);
            if sock.can_recv() {
                let mut err = None;
                while sock.can_recv() && buf.remaining() > 0 {
                    match sock.recv_slice(buf.initialize_unfilled()) {
                        Ok(n) if n > 0 => {
                            buf.advance(n);
                            total += n;
                        }
                        Ok(_) => break,
                        Err(e) => {
                            err = Some(io::Error::new(io::ErrorKind::ConnectionReset, format!("{e:?}")));
                            break;
                        }
                    }
                }
                if let Some(e) = err {
                    return Poll::Ready(Err(e));
                }
                Some(total)
            } else if !sock.may_recv() {
                // 对端已 FIN → EOF
                None
            } else {
                sock.register_recv_waker(cx.waker());
                return Poll::Pending;
            }
        };
        if let Some(n) = n {
            if n > 0 {
                self.stack.poll_now();
            }
        }
        Poll::Ready(Ok(()))
    }
}

impl AsyncWrite for TunTcpStream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        let n = {
            let mut g = lock_inner(&self.stack.inner);
            let sock = g.sockets.get_mut::<tcp::Socket>(self.handle);
            if !sock.may_send() {
                return Poll::Ready(Err(io::Error::from(io::ErrorKind::BrokenPipe)));
            }
            if !sock.can_send() {
                sock.register_send_waker(cx.waker());
                return Poll::Pending;
            }
            sock.send_slice(data)
                .map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, format!("{e:?}")))?
        };
        self.stack.poll_now();
        Poll::Ready(Ok(n))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.stack.poll_now();
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        {
            let mut g = lock_inner(&self.stack.inner);
            g.sockets.get_mut::<tcp::Socket>(self.handle).close();
        }
        self.stack.poll_now();
        Poll::Ready(Ok(()))
    }
}

/// 建立经 Mirage 隧道的出站流 (domain 或裸 IP)。
async fn connect_tunnel(
    stack: &TunStack,
    dst: (std::net::IpAddr, u16),
    direct_domain: Option<String>,
) -> anyhow::Result<Tunnel> {
    use crate::proxy::outbound::OutboundNode;

    let target = if let Some(domain) = direct_domain {
        Address::Domain(domain, dst.1)
    } else {
        Address::Socket(std::net::SocketAddr::new(dst.0, dst.1))
    };

    // 取默认出站的 Mirage pool (与上游 handler.rs 的隧道建立路径一致: 目标头由我们发,
    // 以便拿到 Tunnel 的 reader/writer + 支持 close_notify)。
    let node = stack
        .engine()
        .outbounds
        .get(&stack.engine().default_tag())
        .ok_or_else(|| anyhow::anyhow!("默认出站不存在"))?;
    let leaf = node.resolve_leaf();
    let OutboundNode::Mirage { pool, .. } = &*leaf else {
        anyhow::bail!("默认出站不是 Mirage");
    };

    let mut tunnel = pool.get().await?;
    let hp = target.host_port();
    let tb = hp.as_bytes();
    let mut hdr = Vec::with_capacity(2 + tb.len());
    hdr.extend_from_slice(&(tb.len() as u16).to_be_bytes());
    hdr.extend_from_slice(tb);
    tunnel.writer.send_data(&hdr).await?;
    Ok(tunnel)
}

/// TCP relay 任务入口: 等 Established → 解析目标 → 建隧道 → 双向转发。
pub async fn relay_tcp(stack: Arc<TunStack>, handle: SocketHandle) {
    TCP_ACTIVE.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let _guard = TcpActiveGuard;
    let mut stream = match TunTcpStream::from_catcher(stack.clone(), handle).await {
        Ok(s) => s,
        Err(e) => {
            debug!("[TUN-TCP] 建连失败: {e}");
            return; // stream drop → socket 清理
        }
    };
    let Some(dst) = stream.destination() else {
        warn!("[TUN-TCP] 无法取目标地址, 丢弃连接");
        return;
    };
    debug!("[TUN-TCP] 新连接 → {}", format!("{}:{}", dst.0, dst.1));

    // 分流: fake-IP 反查域名 (DNS 层已决定走向) / 裸 IP (直连标记+CN 段 → 直连)
    let direct_domain = stack.engine().fake_ip_reverse(&dst.0);

    if crate::direct::should_block(direct_domain.as_deref(), Some(dst.0)) {
        let target_name = direct_domain.as_deref().map(|d| d.to_string()).unwrap_or_else(|| dst.0.to_string());
        let (cid, _, _) = crate::monitor::record_conn_start("TCP", &format!("{}:{}", target_name, dst.1), "规则拦截 (Block)");
        crate::monitor::record_conn_close(cid, 0, 0);
        stream.close();
        debug!("[TUN-TCP] 规则拦截: 阻断连接 → {}:{}", target_name, dst.1);
        return;
    }

    let is_direct = match &direct_domain {
        Some(d) => {
            // 域名: 用户规则说直连 → 需要真实 IP; DNS 分流已把直连域名解析为真实 IP
            // (客户端连的就是真实 IP, 不会进 fake-IP)。这里兜底: 有缓存 IP 才直连。
            if crate::direct::should_direct(Some(d), None) {
                match crate::direct::resolve_direct_domain(d) {
                    Some(ip) => {
                        relay_direct(stack.clone(), stream, (ip, dst.1)).await;
                        return;
                    }
                    None => false, // 无真实 IP → fallback 走隧道 (不丢连接)
                }
            } else {
                false
            }
        }
        None => {
            // 裸 IP: 直连标记 (DNS 分流) 或 CN 段 → 直连; 否则代理
            let direct = crate::direct::should_direct(None, Some(dst.0));
            if direct {
                relay_direct(stack.clone(), stream, dst).await;
                return;
            }
            false
        }
    };
    let _ = is_direct;

    let tunnel = match connect_tunnel(&stack, dst, direct_domain.clone()).await {
        Ok(t) => t,
        Err(e) => {
            debug!("[TUN-TCP] 隧道建立失败 ({}:{}): {e}", dst.0, dst.1);
            return;
        }
    };

    let target_name = if let Some(dom) = &direct_domain {
        format!("{}:{}", dom, dst.1)
    } else {
        format!("{}:{}", dst.0, dst.1)
    };
    let (cid, conn_up, conn_down) = crate::monitor::record_conn_start("TCP", &target_name, "隧道代理");

    // 拆成读写半程: upload (app→tunnel) / download (tunnel→app)
    let (mut tun_reader, mut tun_writer) = (tunnel.reader, tunnel.writer);
    let (mut local_rd, mut local_wr) = tokio::io::split(stream);

    let up_atomic = conn_up.clone();
    let upload = async {
        let mut up_bytes: u64 = 0;
        let mut buf = [0u8; 65536];
        loop {
            match tokio::time::timeout(RELAY_IDLE, local_rd.read(&mut buf)).await {
                Ok(Ok(0)) => {
                    let _ = tun_writer.send_close_notify().await;
                    break;
                }
                Ok(Ok(n)) => {
                    if tun_writer.send_data(&buf[..n]).await.is_err() {
                        break;
                    }
                    up_bytes += n as u64;
                    up_atomic.fetch_add(n as u64, std::sync::atomic::Ordering::Relaxed);
                }
                Ok(Err(_)) => {
                    let _ = tun_writer.send_close_notify().await;
                    break;
                }
                Err(_) => break,
            }
        }
        up_bytes
    };

    let down_atomic = conn_down.clone();
    let download = async {
        let mut down_bytes: u64 = 0;
        loop {
            match tokio::time::timeout(RELAY_IDLE, tun_reader.recv_data_to(&mut local_wr)).await {
                Ok(Ok(Some(n))) => {
                    down_bytes += n as u64;
                    down_atomic.fetch_add(n as u64, std::sync::atomic::Ordering::Relaxed);
                }
                Ok(Ok(None)) => break, // 对端正常 close_notify
                Ok(Err(_)) => break,
                Err(_) => break,
            }
        }
        let _ = local_wr.shutdown().await;
        down_bytes
    };

    let (up, down) = tokio::join!(upload, download);
    crate::monitor::record_conn_close(cid, up, down);
    debug!(
        "[TUN-TCP] {}:{} 关闭 (↑{} ↓{})",
        dst.0,
        dst.1,
        crate::tun::udp::human_bytes(up),
        crate::tun::udp::human_bytes(down)
    );
}

#[allow(dead_code)]
fn _sock_buf_const() -> usize {
    SOCK_BUF
}

/// 直连路径: smoltcp socket ⇄ 真实 TCP socket (protect 绕过 TUN)。
async fn relay_direct(_stack: Arc<TunStack>, stream: TunTcpStream, dst: (std::net::IpAddr, u16)) {
    let (cid, conn_up, conn_down) = crate::monitor::record_conn_start("TCP", &format!("{}:{}", dst.0, dst.1), "直连");
    TCP_ACTIVE.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let _guard = TcpActiveGuard;
    use std::os::unix::io::AsRawFd;
    let addr = std::net::SocketAddr::new(dst.0, dst.1);
    let sock = match addr {
        std::net::SocketAddr::V4(_) => tokio::net::TcpSocket::new_v4(),
        std::net::SocketAddr::V6(_) => tokio::net::TcpSocket::new_v6(),
    };
    let sock = match sock {
        Ok(s) => s,
        Err(e) => { debug!("[TUN-TCP/direct] 建 socket 失败: {e}"); return; }
    };
    // protect: 直连 socket 也要绕过 TUN (否则 0.0.0.0/0→tun0 环路)
    crate::protect::protect(sock.as_raw_fd());
    let mut remote = match tokio::time::timeout(
        std::time::Duration::from_secs(15),
        sock.connect(addr),
    ).await {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => { debug!("[TUN-TCP/direct] 连接 {addr} 失败: {e}"); return; }
        Err(_) => { debug!("[TUN-TCP/direct] 连接 {addr} 超时"); return; }
    };
    let _ = remote.set_nodelay(true);

    // 双向转发 (smoltcp 侧 TunTcpStream ↔ 真实 socket)
    let mut local = stream;
    let mut up: u64 = 0;
    let mut down: u64 = 0;
    let (mut lr, mut lw) = tokio::io::split(&mut local);
    let (mut rr, mut rw) = remote.split();
    let up_atomic = conn_up.clone();
    let to_tunnel = async {
        let mut buf = [0u8; 65536];
        loop {
            match lr.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if rw.write_all(&buf[..n]).await.is_err() { break; }
                    up += n as u64;
                    up_atomic.fetch_add(n as u64, std::sync::atomic::Ordering::Relaxed);
                    crate::monitor::add_up(n as u64);
                }
                Err(_) => break,
            }
        }
        up
    };
    let down_atomic = conn_down.clone();
    let from_tunnel = async {
        let mut buf = [0u8; 65536];
        loop {
            match rr.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if lw.write_all(&buf[..n]).await.is_err() { break; }
                    down += n as u64;
                    down_atomic.fetch_add(n as u64, std::sync::atomic::Ordering::Relaxed);
                    crate::monitor::add_down(n as u64);
                }
                Err(_) => break,
            }
        }
        down
    };
    let (up, down) = tokio::join!(to_tunnel, from_tunnel);
    crate::monitor::record_conn_close(cid, up, down);
    debug!("[TUN-TCP/direct] {}:{} 直连关闭 (↑{} ↓{})",
        dst.0, dst.1,
        crate::tun::udp::human_bytes(up), crate::tun::udp::human_bytes(down));
}
