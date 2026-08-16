use crate::proxy::pool::{WarmPool, PoolConfig};
use std::sync::RwLock;
use std::collections::HashMap;
use std::sync::Arc;
use tracing::info;
use crate::config::{Config, OutboundConfig};

pub enum OutboundNode {
    Mirage {
        tag: String,
        pool: Arc<WarmPool>,
        server_host: String,
        server_port: u16,
        server_ip: Arc<RwLock<Option<std::net::IpAddr>>>,
        rtt_ms: Arc<std::sync::atomic::AtomicU64>,
        snd_cwnd: Arc<std::sync::atomic::AtomicU64>,
        total_retrans: Arc<std::sync::atomic::AtomicU64>,
        total_segs_out: Arc<std::sync::atomic::AtomicU64>,
    },
    /// WireGuard / Shadowsocks 出站已在上游移除 (MIRAGE_MOBILE 裁剪: 移动端 v1 只做
    /// Mirage 隧道, 见 docs/ARCHITECTURE.md「裁剪边界」)。
    Direct {
        tag: String,
    },
    Block {
        tag: String,
    },
    Urltest {
        tag: String,
        children: Vec<Arc<OutboundNode>>,
        tolerance_ms: u64,
        test_type: String,
        current: Arc<RwLock<Option<Arc<OutboundNode>>>>,
    },
    Fallback {
        tag: String,
        children: Vec<Arc<OutboundNode>>,
    },
    Selector {
        tag: String,
        children: Vec<Arc<OutboundNode>>,
        current: Arc<RwLock<Option<Arc<OutboundNode>>>>,
    },
    /// 负载均衡组: 把连接**分摊**到多个健康成员 (与 urltest "选一个最优" 不同)。
    /// v1 = round-robin (每连接原子递增取模)。复用 is_healthy 只在健康成员里分摊。
    LoadBalance {
        tag: String,
        children: Vec<Arc<OutboundNode>>,
        /// round-robin 游标 (每次 resolve 递增)。
        next: Arc<std::sync::atomic::AtomicU64>,
    },
}

impl OutboundNode {
    pub fn tag(&self) -> &str {
        match self {
            Self::Mirage { tag, .. } => tag,
            Self::Direct { tag } => tag,
            Self::Block { tag } => tag,
            Self::Urltest { tag, .. } => tag,
            Self::Fallback { tag, .. } => tag,
            Self::Selector { tag, .. } => tag,
            Self::LoadBalance { tag, .. } => tag,
        }
    }

    pub fn is_healthy(self: &Arc<Self>) -> bool {
        match &**self {
            Self::Mirage { pool, .. } => pool.stats.read().unwrap_or_else(|e| e.into_inner()).is_healthy(),
            Self::Direct { .. } | Self::Block { .. } => true,
            Self::Urltest { children, .. } | Self::Fallback { children, .. } | Self::Selector { children, .. } | Self::LoadBalance { children, .. } => {
                children.iter().any(|c| c.is_healthy())
            }
        }
    }

    pub fn latency_rtt_ms(self: &Arc<Self>) -> Option<u64> {
        match &**self {
            Self::Mirage { rtt_ms, .. } => {
                let rtt = rtt_ms.load(std::sync::atomic::Ordering::Relaxed);
                if rtt > 0 && rtt != u64::MAX { Some(rtt) } else { None }
            },
            Self::Direct { .. } | Self::Block { .. } => None,
            Self::Urltest { .. } | Self::Fallback { .. } | Self::Selector { .. } | Self::LoadBalance { .. } => {
                let leaf = self.resolve_leaf();
                if std::ptr::eq(&*leaf, &**self) { None } else { leaf.latency_rtt_ms() }
            }
        }
    }

    pub fn latency_http_ms(self: &Arc<Self>) -> Option<u64> {
        match &**self {
            Self::Mirage { pool, .. } => pool.stats.read().unwrap_or_else(|e| e.into_inner()).latency_ms(),
            Self::Direct { .. } | Self::Block { .. } => None,
            Self::Urltest { .. } | Self::Fallback { .. } | Self::Selector { .. } | Self::LoadBalance { .. } => {
                let leaf = self.resolve_leaf();
                if std::ptr::eq(&*leaf, &**self) { None } else { leaf.latency_http_ms() }
            }
        }
    }

    pub fn latency_ms(self: &Arc<Self>, test_type: &str) -> Option<u64> {
        if test_type == "rtt" {
            self.latency_rtt_ms().or_else(|| self.latency_http_ms())
        } else {
            self.latency_http_ms()
        }
    }

    pub fn resolve_leaf(self: &Arc<Self>) -> Arc<OutboundNode> {
        match &**self {
            Self::Urltest { tag, children, tolerance_ms, test_type, current } => {
                let candidates: Vec<_> = children.iter().filter(|c| c.is_healthy()).collect();
                if candidates.is_empty() {
                    return self.clone();
                }

                let with_lat: Vec<_> = candidates.iter()
                    .filter_map(|c| c.latency_ms(test_type).map(|lat| (c, lat)))
                    .collect();

                if with_lat.is_empty() {
                    let mut curr_guard = current.write().unwrap_or_else(|e| e.into_inner());
                    if let Some(c) = curr_guard.as_ref() {
                        if c.is_healthy() {
                            return c.resolve_leaf();
                        }
                    }
                    *curr_guard = Some(candidates[0].clone());
                    return candidates[0].resolve_leaf();
                }

                let best = with_lat.into_iter()
                    .min_by_key(|&(_, lat)| lat)
                    .unwrap();

                let mut curr_guard = current.write().unwrap_or_else(|e| e.into_inner());
                if let Some(curr) = curr_guard.as_ref() {
                    if let Some(curr_lat) = curr.latency_ms(test_type) {
                        if curr_lat <= best.1 + *tolerance_ms {
                            return curr.resolve_leaf();
                        }
                    }
                }

                info!("Urltest '{}' switched to {}", tag, best.0.tag());
                *curr_guard = Some((*best.0).clone());
                best.0.resolve_leaf()
            }
            Self::Fallback { children, .. } => {
                for c in children {
                    if c.is_healthy() {
                        return c.resolve_leaf();
                    }
                }
                if let Some(first) = children.first() {
                    first.resolve_leaf()
                } else {
                    self.clone()
                }
            }
            Self::Selector { children, current, .. } => {
                let curr_guard = current.read().unwrap_or_else(|e| e.into_inner());
                if let Some(c) = curr_guard.as_ref() {
                    return c.resolve_leaf();
                }
                if let Some(c) = children.first() {
                    return c.resolve_leaf();
                }
                self.clone()
            }
            Self::LoadBalance { children, next, .. } => {
                // round-robin: 只在健康成员里分摊, 原子游标递增取模。
                let healthy: Vec<_> = children.iter().filter(|c| c.is_healthy()).collect();
                if healthy.is_empty() {
                    // 无健康成员: 退回首个 child 去试 (别 self.clone 成死路)。
                    return children.first().map(|c| c.resolve_leaf()).unwrap_or_else(|| self.clone());
                }
                let i = (next.fetch_add(1, std::sync::atomic::Ordering::Relaxed) % healthy.len() as u64) as usize;
                healthy[i].resolve_leaf()
            }
            _ => self.clone(),
        }
    }

    /// 统一出站流接口: 经本出站连到 `target`, 返回一条普通字节流。
    ///
    /// 让**进程内消费者** (geo 下载 / 订阅刷新 / 链式代理) 直接用隧道, 不再绕 SOCKS 入站自连
    /// (见 brain unified-outbound-stream)。组出站先 `resolve_leaf` 选叶子再连。
    /// target 用类型化 [`Address`] (吸收 Gemini 建议): Domain 交由出站/服务端解析, Socket 直用,
    /// 免各处重复解析 host:port 字符串; 也为链式代理 (#5) 的 dialer 注入铺路。
    pub async fn connect(self: &Arc<Self>, target: &Address) -> anyhow::Result<OutStream> {
        let leaf = self.resolve_leaf();
        match &*leaf {
            OutboundNode::Direct { .. } => {
                // Socket 直连 (免解析); Domain 交给 tokio 解析。
                let s = match target {
                    Address::Socket(sa) => tokio::net::TcpStream::connect(sa).await?,
                    Address::Domain(h, p) => tokio::net::TcpStream::connect((h.as_str(), *p)).await?,
                };
                let _ = s.set_nodelay(true);
                Ok(OutStream::Direct(s))
            }
            OutboundNode::Block { tag } => {
                anyhow::bail!("outbound `{tag}` 是 block, 拒绝连接 {target}")
            }
            OutboundNode::Mirage { pool, .. } => {
                let mut tunnel = pool.get().await?;
                // 目标头: [2B len][host:port]; 服务端据此远程解析并连接 (Domain 保留域名交服务端
                // 解析, 抗污染)。与 handler.rs 一致。
                let hp = target.host_port();
                let tb = hp.as_bytes();
                if tb.len() > u16::MAX as usize {
                    anyhow::bail!("target 过长: {} 字节", tb.len());
                }
                let mut hdr = Vec::with_capacity(2 + tb.len());
                hdr.extend_from_slice(&(tb.len() as u16).to_be_bytes());
                hdr.extend_from_slice(tb);
                tunnel.writer.send_data(&hdr).await?;
                Ok(OutStream::Mirage(crate::proxy::mirage_stream::MirageStream::from_tunnel(tunnel)))
            }
            // 移动端裁剪: WG / Shadowsocks 出站已移除 (见 OutboundNode 枚举注释)。
            // resolve_leaf 已把组解到叶子; 仍是组 = 无健康成员可用。
            other => anyhow::bail!("outbound `{}` 无可用叶子出站, 无法连接 {target}", other.tag()),
        }
    }
}

/// 类型化出站目标 (吸收 Gemini 方案): 域名与已解析地址分开, 免各处重复 host:port 字符串解析。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Address {
    /// 域名 + 端口。交由出站远程解析 (Mirage 服务端 / WG 隧道内 DNS), 抗本地 DNS 污染。
    Domain(String, u16),
    /// 已解析的 socket 地址 (v4/v6)。
    Socket(std::net::SocketAddr),
}

impl Address {
    /// 解析 `host:port` / `[v6]:port` / `ip:port`: 纯 IP → Socket, 否则 Domain。
    pub fn parse(s: &str) -> anyhow::Result<Address> {
        // 完整 socket 地址 (含 ip:port / [v6]:port) 优先。
        if let Ok(sa) = s.parse::<std::net::SocketAddr>() {
            return Ok(Address::Socket(sa));
        }
        // 否则拆 host:port ([v6]:port 或域名:port)。
        let (host, port) = if let Some(rest) = s.strip_prefix('[') {
            rest.split_once("]:")
                .ok_or_else(|| anyhow::anyhow!("非法 [v6]:port: {s}"))?
        } else {
            s.rsplit_once(':')
                .ok_or_else(|| anyhow::anyhow!("target 缺端口 (需 host:port): {s}"))?
        };
        if host.is_empty() {
            anyhow::bail!("target host 为空: {s}");
        }
        Ok(Address::Domain(host.to_string(), port.parse()?))
    }

    /// 端口。
    pub fn port(&self) -> u16 {
        match self {
            Address::Domain(_, p) => *p,
            Address::Socket(sa) => sa.port(),
        }
    }

    /// `host:port` 串 (Mirage 目标头 / 日志用); v6 socket 自带方括号。
    pub fn host_port(&self) -> String {
        match self {
            Address::Domain(h, p) => crate::net_util::join_host_port(h, *p),
            Address::Socket(sa) => sa.to_string(),
        }
    }
}

impl std::fmt::Display for Address {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.host_port())
    }
}

/// 统一出站字节流。闭集枚举 (无 vtable); 各变体都 Unpin, poll 委托直接 `Pin::new`。
pub enum OutStream {
    Direct(tokio::net::TcpStream),
    Mirage(crate::proxy::mirage_stream::MirageStream),
}

impl tokio::io::AsyncRead for OutStream {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match &mut *self {
            OutStream::Direct(s) => std::pin::Pin::new(s).poll_read(cx, buf),
            OutStream::Mirage(s) => std::pin::Pin::new(s).poll_read(cx, buf),
        }
    }
}

impl tokio::io::AsyncWrite for OutStream {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        match &mut *self {
            OutStream::Direct(s) => std::pin::Pin::new(s).poll_write(cx, buf),
            OutStream::Mirage(s) => std::pin::Pin::new(s).poll_write(cx, buf),
        }
    }
    fn poll_flush(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match &mut *self {
            OutStream::Direct(s) => std::pin::Pin::new(s).poll_flush(cx),
            OutStream::Mirage(s) => std::pin::Pin::new(s).poll_flush(cx),
        }
    }
    fn poll_shutdown(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match &mut *self {
            OutStream::Direct(s) => std::pin::Pin::new(s).poll_shutdown(cx),
            OutStream::Mirage(s) => std::pin::Pin::new(s).poll_shutdown(cx),
        }
    }
}

pub struct OutboundManager {
    pub outbounds: HashMap<String, Arc<OutboundNode>>,
}

impl OutboundManager {
    /// 建一个 Mirage 出站节点 (含 WarmPool)。`underlying` 为链式代理 (Mirage-over-X) 的底层出站,
    /// None = 直连。抽出以便 Pass 1 (无 underlying) 与 Pass 2 (依赖 underlying 已建) 复用。
    fn build_mirage(oc: &OutboundConfig, underlying: Option<Arc<OutboundNode>>) -> Arc<OutboundNode> {
        let OutboundConfig::Mirage {
            tag, server, server_port, password, camouflage_host, pool_size,
            brutal_rate_mbps, brutal_base_rtt_ms, pfs, ..
        } = oc else { unreachable!("build_mirage 只接受 Mirage 配置") };
        let pool_cfg = Arc::new(PoolConfig {
            server_host: server.clone(),
            server_port: *server_port,
            password: password.clone(),
            camouflage_host: camouflage_host.clone(),
            pool_size: *pool_size,
            underlying,
            pfs: *pfs,
        });
        let bytes_per_sec = brutal_rate_mbps.map(|m| m * 125_000);
        let brutal_state = Arc::new(crate::proxy::pool::BrutalState {
            configured_rate: bytes_per_sec,
            current_rate: Arc::new(std::sync::atomic::AtomicU64::new(bytes_per_sec.unwrap_or(8_000_000))),
            base_rtt: *brutal_base_rtt_ms,
            active_fds: Arc::new(std::sync::Mutex::new(std::collections::HashSet::new())),
        });
        let pool = Arc::new(WarmPool::new(pool_cfg, brutal_state));
        Arc::new(OutboundNode::Mirage {
            tag: tag.clone(),
            pool,
            server_host: server.clone(),
            server_port: *server_port,
            server_ip: Arc::new(RwLock::new(None)),
            rtt_ms: Arc::new(std::sync::atomic::AtomicU64::new(u64::MAX)),
            snd_cwnd: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            total_retrans: Arc::new(std::sync::atomic::AtomicU64::new(u64::MAX)),
            total_segs_out: Arc::new(std::sync::atomic::AtomicU64::new(u64::MAX)),
        })
    }

    pub fn new(cfg: &Config) -> anyhow::Result<Self> {
        let mut outbounds = HashMap::new();
        let mut deferred = Vec::new();

        // Pass 1: Leaf nodes
        for oc in &cfg.outbounds {
            match oc {
                OutboundConfig::Mirage { tag, underlying, .. } => {
                    // 无 underlying → 直连 Mirage, Pass 1 立即建。有 underlying → 依赖另一出站,
                    // 延后到 Pass 2 (等 underlying 建好再注入)。
                    if underlying.is_some() {
                        deferred.push(oc);
                    } else {
                        outbounds.insert(tag.clone(), Self::build_mirage(oc, None));
                    }
                }
                OutboundConfig::Direct { tag } => {
                    outbounds.insert(tag.clone(), Arc::new(OutboundNode::Direct { tag: tag.clone() }));
                }
                OutboundConfig::Block { tag } => {
                    outbounds.insert(tag.clone(), Arc::new(OutboundNode::Block { tag: tag.clone() }));
                }
                _ => {
                    deferred.push(oc);
                }
            }
        }

        // Auto-add implicit direct and block if not present
        if !outbounds.contains_key("direct") {
            outbounds.insert("direct".to_string(), Arc::new(OutboundNode::Direct { tag: "direct".to_string() }));
        }
        if !outbounds.contains_key("block") {
            outbounds.insert("block".to_string(), Arc::new(OutboundNode::Block { tag: "block".to_string() }));
        }

        // Pass 2: Group nodes (Urltest, Fallback) - simplified fixpoint resolution
        let mut pending = deferred;
        while !pending.is_empty() {
            let mut progress = false;
            let mut next_round = Vec::new();

            for oc in pending {
                // Mirage-over-X: 依赖 underlying 出站已建, 建好则注入并建本节点, 否则下一轮再试。
                if let OutboundConfig::Mirage { tag, underlying: Some(utag), .. } = oc {
                    match outbounds.get(utag) {
                        Some(u) => {
                            let u = u.clone();
                            outbounds.insert(tag.clone(), Self::build_mirage(oc, Some(u)));
                            progress = true;
                        }
                        None => next_round.push(oc),
                    }
                    continue;
                }

                let mut hc_test_type = "ping".to_string();
                let (tag, child_tags, otype, _interval, tolerance) = match oc {
                    OutboundConfig::Urltest { tag, outbounds, interval, tolerance, url, test_type } => {
                        hc_test_type = test_type.clone();
                        let _ = (url, interval);
                        (tag, outbounds, "urltest", *interval, *tolerance)
                    }
                    OutboundConfig::Fallback { tag, outbounds, interval, url } => {
                        let _ = (url, interval);
                        (tag, outbounds, "fallback", *interval, 0)
                    }
                    OutboundConfig::Selector { tag, outbounds } => {
                        (tag, outbounds, "selector", 0, 0)
                    }
                    OutboundConfig::LoadBalance { tag, outbounds, url, interval, .. } => {
                        let _ = (url, interval);
                        (tag, outbounds, "load_balance", *interval, 0)
                    }
                    _ => unreachable!(),
                };

                let mut children = Vec::new();
                let mut resolved = true;
                for ct in child_tags {
                    if let Some(node) = outbounds.get(ct) {
                        children.push(node.clone());
                    } else {
                        resolved = false;
                        break;
                    }
                }

                if resolved {
                    // 移动端裁剪: 健康检查模块未 vendored (urltest 组靠 is_healthy/stale 探测兜底)。
                    let node = if otype == "urltest" {
                        Arc::new(OutboundNode::Urltest {
                            tag: tag.clone(),
                            children,
                            tolerance_ms: tolerance,
                            test_type: hc_test_type,
                            current: Arc::new(RwLock::new(None)),
                        })
                    } else if otype == "selector" {
                        Arc::new(OutboundNode::Selector {
                            tag: tag.clone(),
                            children,
                            current: Arc::new(RwLock::new(None)),
                        })
                    } else if otype == "load_balance" {
                        Arc::new(OutboundNode::LoadBalance {
                            tag: tag.clone(),
                            children,
                            next: Arc::new(std::sync::atomic::AtomicU64::new(0)),
                        })
                    } else {
                        Arc::new(OutboundNode::Fallback {
                            tag: tag.clone(),
                            children,
                        })
                    };
                    outbounds.insert(tag.clone(), node);
                    progress = true;
                } else {
                    next_round.push(oc);
                }
            }

            if !progress {
                // 配置错误 (未解析 / 循环出站组): 返回诊断错误而非 panic 杀进程。
                anyhow::bail!(
                    "outbound 组无法解析或存在循环引用: {:?} (检查这些 group 的 children 是否都指向已定义的出站, 且无相互/自我引用)",
                    next_round
                );
            }
            pending = next_round;
        }

        Ok(Self { outbounds })
    }

    pub fn get(&self, tag: &str) -> Option<Arc<OutboundNode>> {
        self.outbounds.get(tag).cloned()
    }
}

