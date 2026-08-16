//! 待 protect 的 socket fd 队列。
//!
//! `VpnService.protect(fd)` 只能在 Java 侧调用 (需要 VpnService 实例), 而隧道 socket
//! 由 Rust 的 tokio worker 线程创建 (未附加 JVM)。因此:
//! 1. Rust 侧 (pool.rs 建连后) 把 fd 推入本队列
//! 2. Kotlin 侧定时 (如每 200ms) 调 `MirageNative.drainProtectFds()` 取走并调
//!    `vpnService.protect(fd)`
//!
//! 队列有上限: 极端情况下 (瞬时大量建连) 丢弃多余 fd 也无妨 —— protect 是防环路的
//! 兜底, 少量未 protect 的 socket 只会在该连接持续期间消耗一点路由开销, 不致命。

use std::collections::VecDeque;
use std::sync::Mutex;

static QUEUE: Mutex<VecDeque<i32>> = Mutex::new(VecDeque::new());
const CAP: usize = 512;

pub fn push(fd: i32) {
    let mut q = QUEUE.lock().unwrap_or_else(|e| e.into_inner());
    if q.len() < CAP {
        q.push_back(fd);
    }
}

pub fn drain() -> Vec<i32> {
    let mut q = QUEUE.lock().unwrap_or_else(|e| e.into_inner());
    q.drain(..).collect()
}

pub fn clear() {
    let mut q = QUEUE.lock().unwrap_or_else(|e| e.into_inner());
    q.clear();
}
