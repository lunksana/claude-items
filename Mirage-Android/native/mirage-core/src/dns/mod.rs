//! 移动端裁剪版 dns 模块 (vendored from mirage-rs)。
//!
//! 只保留 fake-IP 映射器 (TUN DNS 反查域名用)。上游的完整 DNS 服务端 / XDP /
//! 防风暴 / TTL 缓存等均在移动端裁剪掉。

pub mod fake_ip;
