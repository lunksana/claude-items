//! 移动端裁剪版 proxy 模块 (vendored from mirage-rs, MIRAGE_MOBILE 裁剪标记)。
//!
//! 与上游 `src/proxy/mod.rs` 的差异: 只保留移动端数据面需要的子模块。
//! 裁剪掉: socks5/handler/udp_relay (SOCKS 入站, 移动端用 TUN 直连) /
//! mirage_server (服务端) / transparent* (eBPF 透明网关) / wg / shadowsocks /
//! ss_inbound / ss_stream / mixed / healthcheck / udp_mux / sniff / splice /
//! upstream / internal_socks / probe / proc_lookup。

pub mod brutal;
pub mod mirage_stream;
pub mod outbound;
pub mod pool;
pub mod resolver;
pub mod tunnel;
pub mod udp_mux;
