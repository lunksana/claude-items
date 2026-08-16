//! 隧道 socket 保护钩子。
//!
//! Android 的 VPNService 要求: 代理软件自己的出站 socket 必须 `VpnService.protect(fd)`,
//! 否则隧道流量会重新被路由进 TUN 造成环路。iOS 的 `NEPacketTunnelProvider` 无此要求
//! (隧道 socket 默认走真实网络)。
//!
//! 各平台包装层 (mirage-jni) 在启动时注册回调; 内核 (proxy::pool) 在每建一条隧道 socket
//! 后立即调用。未注册时静默跳过 (桌面端/Linux 无此概念)。

use std::sync::OnceLock;

/// 保护回调: 输入原始 socket fd, 返回 ()。
pub type ProtectFn = Box<dyn Fn(i32) + Send + Sync>;

static PROTECT: OnceLock<ProtectFn> = OnceLock::new();

/// 注册保护回调 (幂等, 重复注册忽略)。
pub fn set_protect_callback(f: ProtectFn) {
    let _ = PROTECT.set(f);
}

/// 清空 (测试用)。
pub fn clear_protect_callback() {
    // OnceLock 无法重置; 用哨兵函数替代 (见下)
    let _ = PROTECT.set(Box::new(|_| {}));
}

/// 是否已注册。
pub fn is_set() -> bool {
    PROTECT.get().is_some()
}

/// 保护一个 socket (未注册时无操作)。
pub fn protect(fd: i32) {
    if let Some(f) = PROTECT.get() {
        f(fd);
    }
}
