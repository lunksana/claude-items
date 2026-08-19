//! 移动端裁剪版配置类型 (vendored from mirage-rs, MIRAGE_MOBILE 裁剪标记)。
//!
//! 与上游 `config.rs` 的差异:
//!   - 移除: gui / api / inbounds / routing / tuning / geo / dns 全套配置 (移动端由
//!     App UI 直控, 不走配置文件)
//!   - 出站保留: Mirage / Direct / Block / Selector / Urltest / Fallback / LoadBalance
//!   - 移除: Wireguard / Shadowsocks (移动端 v1 只做 Mirage 隧道)
//!   - 保留上游字段语义与默认值, 便于协议侧零改动复用

use serde::Deserialize;

/// 出站配置。移动端 v1: Mirage (隧道) / Direct / Block / 组。
#[derive(Debug, Clone, Deserialize)]
pub enum OutboundConfig {
    Mirage {
        tag: String,
        server: String,
        server_port: u16,
        password: String,
        /// 伪装 SNI, 必须与服务端一致。
        #[serde(default = "d_camouflage_host")]
        camouflage_host: String,
        #[serde(default = "d_pool_size")]
        pool_size: usize,
        /// Brutal 拥塞控制目标速率 (Mbps)。移动端默认为 None (Android 内核无 brutal CC 模块,
        /// 配了也会在运行时安全跳过, 见 proxy::brutal)。
        #[serde(default)]
        brutal_rate_mbps: Option<u64>,
        #[serde(default)]
        brutal_base_rtt_ms: Option<u64>,
        /// 前向保密: 握手做一次性 X25519 ECDH。须与服务端 `pfs` 同开。默认关。
        #[serde(default)]
        pfs: bool,
        /// UDP 多路复用开关 (默认 true)。
        #[serde(default = "d_true")]
        udp_mux: bool,
        #[serde(default = "d_udp_mux_tunnels")]
        udp_mux_tunnels: usize,
        /// 链式代理底层出站 (Mirage-over-X)。移动端 v1 不支持, 保留字段以兼容上游配置解析。
        #[serde(default)]
        underlying: Option<String>,
    },
    Direct { tag: String },
    Block { tag: String },
    /// 手动选择组。
    Selector {
        tag: String,
        outbounds: Vec<String>,
    },
    /// 自动选延迟最低。
    Urltest {
        tag: String,
        outbounds: Vec<String>,
        #[serde(default)]
        interval: u64,
        #[serde(default)]
        tolerance: u64,
        #[serde(default)]
        url: String,
        #[serde(default = "d_test_type")]
        test_type: String,
    },
    /// 第一个健康成员。
    Fallback {
        tag: String,
        outbounds: Vec<String>,
        #[serde(default)]
        interval: u64,
        #[serde(default)]
        url: String,
    },
    /// 轮询分摊。
    LoadBalance {
        tag: String,
        outbounds: Vec<String>,
        #[serde(default)]
        url: String,
        #[serde(default)]
        interval: u64,
    },
}

fn d_camouflage_host() -> String { "www.apple.com".into() }
fn d_pool_size() -> usize { 4 }
fn d_test_type() -> String { "ping".into() }
fn d_true() -> bool { true }
fn d_udp_mux_tunnels() -> usize { 4 }

/// 最简 Config: 只有出站表。移动端 engine 直接用它构建 CoreState。
#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub outbounds: Vec<OutboundConfig>,
}

/// 从单节点信息构造一个"单 Mirage 出站"的 Config (轻量模式语义, 全部转发)。
pub fn single_mirage_config(
    tag: &str,
    server: &str,
    server_port: u16,
    password: &str,
    camouflage_host: &str,
    pool_size: usize,
    pfs: bool,
    udp_mux: bool,
) -> Config {
    Config {
        outbounds: vec![
            OutboundConfig::Mirage {
                tag: tag.to_string(),
                server: server.to_string(),
                server_port,
                password: password.to_string(),
                camouflage_host: camouflage_host.to_string(),
                pool_size,
                brutal_rate_mbps: None,
                brutal_base_rtt_ms: None,
                pfs,
                udp_mux,
                udp_mux_tunnels: 4,
                underlying: None,
            },
            OutboundConfig::Direct { tag: "direct".to_string() },
            OutboundConfig::Block { tag: "block".to_string() },
        ],
    }
}
