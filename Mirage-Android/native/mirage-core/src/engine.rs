//! 移动端引擎: 持有出站管理 + fake-IP 映射, 提供 TUN 数据面需要的连接 API。
//!
//! 对应上游 lite.rs 的 `build_core_state` (单 Mirage 出站 + 全转发), 但更薄:
//! 没有 config_watcher / 热重载 / 看板, 配置由 App 通过 JNI 一次性注入。

use anyhow::{Context, Result};
use std::net::IpAddr;
use std::sync::Arc;

use crate::config::{self, Config};
use crate::dns::fake_ip::FakeIpMapper;
use crate::proxy::outbound::{Address, OutboundManager, OutboundNode, OutStream};

/// fake-IP 网段。与上游透明网关默认一致 (RFC 1918 之外的保留段, Android 路由表会把它
/// 指进 TUN)。**/16** 而非上游的 /15: 把 198.19.0.0/16 留给 TUN DNS 地址, 避免 DNS
/// 地址与 fake-IP 分配碰撞 (映射器从 198.18.0.2 顺序分配, /16 提供 6.5 万域名, 手机
/// 单机绰绰有余)。
pub const FAKE_IP_CIDR: &str = "198.18.0.0/16";
/// TUN 接口地址 (点对点, /32)。**不能**同时也是 DNS 地址: 分配到接口的地址会被内核
/// 当本地地址直接投递 (回 ICMP 端口不可达), 包根本进不了 TUN。
pub const TUN_ADDR_V4: &str = "198.18.0.1";
/// TUN DNS 服务器地址 (App 端 VpnService.Builder.addDnsServer 用同一地址)。
/// 取 fake-IP /16 之外的 198.19.0.53 —— 该地址经默认路由进 TUN, 由引擎的 DNS 应答器
/// 处理 (见 tun::dns)。
pub const TUN_DNS_V4: &str = "198.19.0.53";
/// TUN 对端地址 (点对点网关, smoltcp 默认路由下一跳)。
pub const TUN_PEER_V4: &str = "198.18.0.2";

/// 单节点连接信息 (App 从 mirage:// URI 或手工表单解析而来)。
#[derive(Debug, Clone)]
pub struct NodeInfo {
    pub tag: String,
    pub server: String,
    pub server_port: u16,
    pub password: String,
    pub sni: String,
    pub pool_size: usize,
    pub pfs: bool,
    pub udp_mux: bool,
}

impl NodeInfo {
    pub fn from_node_uri(uri: &crate::node_uri::NodeUri) -> Self {
        Self {
            tag: format!("proxy-{}", uri.port),
            server: uri.host.clone(),
            server_port: uri.port,
            password: uri.password.clone(),
            sni: uri.sni.clone(),
            pool_size: uri.pool_size.unwrap_or(8),
            pfs: false,
            udp_mux: uri.udp_mux.unwrap_or(true),
        }
    }
}

/// 移动端引擎。全部字段 Arc, 可跨线程共享。
pub struct Engine {
    pub outbounds: Arc<OutboundManager>,
    pub fake_ip: Arc<FakeIpMapper>,
    /// 默认出站 tag (单节点 = 那个 Mirage 出站的 tag)。
    default_tag: String,
}

impl Engine {
    pub fn new(node: &NodeInfo) -> Result<Arc<Self>> {
        crate::proxy::udp_mux::set_udp_mux(node.udp_mux, 4);
        let cfg = config::single_mirage_config(
            &node.tag, &node.server, node.server_port, &node.password,
            &node.sni, node.pool_size, node.pfs, node.udp_mux,
        );
        let mgr = OutboundManager::new(&cfg).context("构建出站失败")?;
        let fake_ip = FakeIpMapper::new(FAKE_IP_CIDR).context("初始化 fake-IP 失败")?;
        Ok(Arc::new(Self {
            outbounds: Arc::new(mgr),
            fake_ip: Arc::new(fake_ip),
            default_tag: node.tag.clone(),
        }))
    }

    /// 用一组配置 (多节点/组) 构建引擎; default_tag 是默认出站。
    pub fn from_config(cfg: Config, default_tag: &str) -> Result<Arc<Self>> {
        let mgr = OutboundManager::new(&cfg).context("构建出站失败")?;
        let fake_ip = FakeIpMapper::new(FAKE_IP_CIDR).context("初始化 fake-IP 失败")?;
        Ok(Arc::new(Self {
            outbounds: Arc::new(mgr),
            fake_ip: Arc::new(fake_ip),
            default_tag: default_tag.to_string(),
        }))
    }

    /// 默认出站 tag。
    pub fn default_tag(&self) -> &str {
        &self.default_tag
    }

    /// 经默认出站连接目标 (域名交服务端解析, 抗污染)。
    pub async fn connect(&self, target: &Address) -> Result<OutStream> {
        let node = self
            .outbounds
            .get(&self.default_tag)
            .ok_or_else(|| anyhow::anyhow!("默认出站 `{}` 不存在", self.default_tag))?;
        node.connect(target).await
    }

    /// fake-IP 反查域名 (TUN 收到去 fake-IP 的 TCP/UDP 时还原真实目标)。
    pub fn fake_ip_reverse(&self, ip: &IpAddr) -> Option<String> {
        if let IpAddr::V4(v4) = ip {
            self.fake_ip.lookup_domain(v4)
        } else {
            None
        }
    }

    /// 分配 fake-IP (DNS 应答用)。返回 None 表示网段耗尽。
    pub fn fake_ip_allocate(&self, domain: &str) -> Option<std::net::Ipv4Addr> {
        Some(self.fake_ip.lookup_or_assign(domain))
    }

    /// 连接池健康度 (App 状态栏显示)。
    pub fn is_healthy(&self) -> bool {
        self.outbounds
            .get(&self.default_tag)
            .map(|n| n.is_healthy())
            .unwrap_or(false)
    }

    /// RTT 毫秒 (App 状态栏显示)。
    pub fn latency_ms(&self) -> Option<u64> {
        self.outbounds.get(&self.default_tag).and_then(|n| n.latency_rtt_ms())
    }

    /// 动态热更新连接池容量 (直接修改 WarmPool 运行时参数，无锁秒级生效)
    pub fn set_pool_size(&self, new_size: usize) {
        self.outbounds.set_pool_size(new_size);
    }

    /// 重置 Fake-IP 映射与直连 DNS 缓存 (VPN 启动/重连时清理历史残留)
    pub fn reset_dns_and_fake_ip(&self) {
        self.fake_ip.clear();
        crate::tun::dns::clear_direct_cache();
        tracing::info!("[Engine] Fake-IP 映射与直连 DNS 缓存已重置");
    }
}

/// 出站列表快照 (App 展示用)。
pub fn list_outbounds(eng: &Engine) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for (tag, node) in eng.outbounds.outbounds.iter() {
        let kind = match &**node {
            OutboundNode::Mirage { .. } => "mirage",
            OutboundNode::Direct { .. } => "direct",
            OutboundNode::Block { .. } => "block",
            OutboundNode::Urltest { .. } => "urltest",
            OutboundNode::Fallback { .. } => "fallback",
            OutboundNode::Selector { .. } => "selector",
            OutboundNode::LoadBalance { .. } => "load_balance",
        };
        out.push((tag.clone(), kind.to_string()));
    }
    out
}
