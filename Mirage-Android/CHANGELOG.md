# Mirage-Android 变更记录与修改日志 (CHANGELOG)

本文档记录 Mirage-Android 项目从代码审计、真机测试、连接稳定性调优、FD 泄露排查以及系统生命周期管理的所有改动细节与技术背景，便于后续版本对齐与追溯。

---

## [2026-08-20] 移动端连接稳定性、生命周期与 FD 泄露彻底调优

### 1. 根治 `flushPool` 切网跨线程 Panic 闪退 (Critical)
* **涉及文件**：
  * [`native/mirage-core/src/proxy/pool.rs`](native/mirage-core/src/proxy/pool.rs)
  * [`native/mirage-core/src/proxy/outbound.rs`](native/mirage-core/src/proxy/outbound.rs)
  * [`native/mirage-core/src/engine.rs`](native/mirage-core/src/engine.rs)
  * [`native/mirage-core/src/tun/mod.rs`](native/mirage-core/src/tun/mod.rs)
  * [`native/mirage-jni/src/lib.rs`](native/mirage-jni/src/lib.rs)
* **背景与根因**：
  * Android 端由 `ConnectivityManager.NetworkCallback` 系统 Binder 回调线程触发 `MirageNative.flushPool()`。
  * 该线程未绑定 Tokio Runtime 上下文，原有 `purge_idle()` 中调用 `tokio::spawn` 导致 Tokio 抛出 Panic：`there is no reactor running, must be called from the context of a Tokio 1.x runtime`。Rust Panic 越过 JNI 边界导致进程直接 abort 闪退。
* **修改方案**：
  * 将 `WarmPool::purge_idle()` 重构成完全同步化：利用 `self.queue.try_lock()` 直接执行 `q.clear()` 同步释放失效 Socket，并调用 `self.notify.notify_waiters()` 唤醒等待任务。
  * 零 Runtime 异步上下文依赖，线程绝对安全。

### 2. 根治 `too many open files` 文件描述符 (FD) 泄露
* **涉及文件**：
  * [`native/mirage-core/src/tun/tcp.rs`](native/mirage-core/src/tun/tcp.rs)
  * [`native/mirage-core/src/tun/udp.rs`](native/mirage-core/src/tun/udp.rs)
* **背景与根因**：
  * **TCP 30分钟挂死**：原 `RELAY_IDLE` 设为 1800s 且使用 `tokio::join!(upload, download)`。短连接（HTTP）客户端 FIN 断开后，服务端任务仍在 1800s 内阻塞等待，导致底层的 Socket FD 无法及时释放，短时间累积数百连接打满系统 FD 上限。
  * **UDP 流表泄露**：`UdpEngine::feed` 插入 4 元组条目，但中继任务结束时未从哈希表中移除，导致流数达到 `MAX_FLOWS` (128) 后新 UDP 请求全部被丢弃。
* **修改方案**：
  * `relay_tcp` 和 `relay_direct` 改用 `tokio::select!` + `tokio::pin!` 竞争感知机制：一旦客户端断开，给服务端 3 秒接收残余数据后立即关闭 Socket 并释放 FD；服务端断开时给客户端 2 秒缓冲发送窗口。
  * 隧道代理连接空闲超时设为 **300s**（保护 SSH/IM 长连接），国内直连设为 **1800s**。
  * 引入 `FlowGuard` RAII 机制，UDP 流任务在任何退出路径均自动触发 `remove_flow`。

### 3. 移动蜂窝网络 TCP KeepAlive 显式配置
* **涉及文件**：
  * [`native/mirage-core/src/proxy/pool.rs`](native/mirage-core/src/proxy/pool.rs)
* **背景与根因**：
  * Linux 默认的 `TCP_KEEPIDLE` 为 7200 秒（2 小时），对移动运营商基站 60s ~ 120s 的 NAT 表超时完全无效，导致手机待机或短暂停顿后预热连接坏死。
* **修改方案**：
  * 通过 `libc::setsockopt` 显式设置运营商级保活参数：
    * `TCP_KEEPIDLE = 45s`（45秒无数据开始探测，避开 60s 阈值）
    * `TCP_KEEPINTVL = 10s`（探测间隔 10秒）
    * `TCP_KEEPCNT = 3`（重试 3次）

### 4. 修复通知栏下滑「断开连接」需点击两次的缺陷
* **涉及文件**：
  * [`android/app/src/main/java/com/mirage/android/CoreService.kt`](android/app/src/main/java/com/mirage/android/CoreService.kt)
* **背景与根因**：
  * `CoreService.onStartCommand` 返回了 `START_STICKY`。当通知栏点击断开触发 `stopSelf()` 后，Android 系统的 AMS 会立即以 `intent = null` 重启服务并重新弹窗通知。
* **修改方案**：
  * 将 `onStartCommand` 返回值修改为 `START_NOT_STICKY`；
  * 增加 `if (intent == null) return START_NOT_STICKY` 空意图防御；
  * 在 `stopInternal()` 入口处第一时间执行 `clearActive()`。

### 5. 移动端后台省电与生命周期数据平滑持久化
* **涉及文件**：
  * [`android/app/src/main/java/com/mirage/android/CoreService.kt`](android/app/src/main/java/com/mirage/android/CoreService.kt)
* **修改方案**：
  * 注册 `ConnectivityManager.NetworkCallback` 监听 Wi-Fi <-> 蜂窝网络切换，动态触发 `MirageNative.flushPool()`；
  * `logJob` 磁盘落盘频率从 3s 降低为 60s，`trafficJob` 调整为 30s，允许 SoC 进入 Deep Sleep 深睡模式；
  * 抽取 `flushLogsAndStats()`，在服务正常停止（`stopInternal`）与系统异常销毁（`onDestroy`）时均触发一次完整的增量落盘，防止数据丢失。

---

## [2026-08-19] 代码审计 R1-R3 缺陷修复与稳定性提升

### 1. R1: CN_IPV4 二分查找区间重叠修复 (Critical)
* **涉及文件**：[`native/mirage-core/src/direct.rs`](native/mirage-core/src/direct.rs)
* **问题**：`CN_IPV4` 内置列表存在 176 处重叠或相邻网段，导致 `binary_search_by` 谓词非传递，部分 IP（如 `114.114.114.114`）命中失败。
* **修复**：`LazyLock` 初始化时执行单调区间排序与重叠区间合并，100% 确保互斥二分。

### 2. R2: 域名分流大小写敏感问题修复
* **涉及文件**：[`native/mirage-core/src/direct.rs`](native/mirage-core/src/direct.rs)
* **修复**：在匹配前统一进行 `to_ascii_lowercase()` 规范化。

### 3. R3: 示例与单元测试参数缺失修复
* **涉及文件**：[`native/mirage-core/examples/tun_e2e.rs`](native/mirage-core/examples/tun_e2e.rs), [`native/mirage-core/examples/tunnel_test.rs`](native/mirage-core/examples/tunnel_test.rs)
* **修复**：补充 `udp_mux: true` 构造字段。

### 4. M3: 节点测速与 Failover 并行化
* **涉及文件**：[`android/app/src/main/java/com/mirage/android/CoreService.kt`](android/app/src/main/java/com/mirage/android/CoreService.kt)
* **修复**：改用 `Dispatchers.IO` + `async/awaitAll` 并发测速，将 3 个节点的探测耗时从 9s 缩短至 1s。

### 5. 冗余代码清理
* 清理 JNI `drainProtectFds` 废弃符号；
* 消除 `CoreService.kt` 中重复注册的动态广播接收器。
