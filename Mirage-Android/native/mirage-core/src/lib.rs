//! # mirage-core — Mirage-rs 移动端裁剪内核
//!
//! 从 [`mirage-rs`](https://github.com/zdgt0226/Mirage-rs) 裁剪出来的**平台无关**协议核心,
//! 专为 Android / iOS 移动客户端设计:
//!
//! ```text
//! App (Kotlin/Swift UI + VpnService/NEPacketTunnelProvider)
//!   │  JNI / FFI
//!   ▼
//! [本 crate] mirage-core
//!   ├── vendored 协议内核 (crypto / pool / outbound / tunnel / fake_ip / node_uri)
//!   │      └─ 与 mirage-rs 上游协议逐字节兼容 (TLS1.3 仿真握手 / ChaCha20-Poly1305 分帧 / PFS)
//!   └── tun 引擎 (smoltcp) —— TUN 接口 → smoltcp 用户态协议栈 → Mirage 隧道
//!
//! 裁剪边界见 `docs/ARCHITECTURE.md`; vendored 代码同步见 `native/vendor-sync.sh`。
//!
//! ## 平台接入
//! - Android: `native/mirage-jni` (JNI cdylib, 拿 VpnService 的 TUN fd + protect 回调)
//! - iOS:     `native/mirage-ios` (规划中, 见 `docs/IOS_PORTING.md`)

pub mod config;
pub mod crypto;
pub mod direct;
pub mod dns;
pub mod engine;
pub mod net_util;
pub mod node_uri;
pub mod proxy;
pub mod time_sync;
pub mod monitor;
pub mod protect;
pub mod tun;

/// 版本信息 (跟随 vendored 协议版本, 见 vendor/SYNC.md)。
pub const PROTOCOL_SYNC: &str = include_str!("vendor/SYNC.md");
