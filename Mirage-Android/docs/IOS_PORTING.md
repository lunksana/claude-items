# iOS 移植指南

本文是 Android 客户端 → iOS 的完整移植路径。**核心结论: `mirage-core` 已平台无关, iOS
只需要新增一层 Swift 包装 + 一个 TUN 数据源适配, 协议内核零改动。**

## 0. 可行性评估

| 依赖 | Android 现状 | iOS 评估 |
|---|---|---|
| Rust 目标 | aarch64-linux-android | `aarch64-apple-ios` (rustup target add 即可) |
| smoltcp | ✅ | ✅ 纯 Rust, 平台无关 |
| ring/aws-lc (C 加密) | ✅ NDK clang | ✅ 需 iOS SDK clang (Xcode) |
| tokio | ✅ | ✅ |
| libc | ✅ bionic | ⚠️ 需 `target_os="ios"` 适配 (少量, 见下) |
| JNI | ✅ | ❌ **不使用** — 改用 C ABI / swift-bridge |
| TUN 数据源 | VpnService fd | `NEPacketTunnelProvider` (不同 API, 需适配层) |
| socket protect | 必须 (防环路) | **不需要** (隧道 socket 默认走真实网络) |
| netlink 链路自愈 | 已裁剪 | 已裁剪 (cfg 门控已就位) |

**工作量估计**: mirage-core 零改动 (已验证 `cfg(target_os="ios")` 门控); 新增
`native/mirage-ios` (C ABI 包装, ~300 行) + `tun/device` 的 iOS 适配层 (~100 行) +
Swift App (UI + NEPacketTunnelProvider, ~500 行)。

## 1. 总体结构

```
Mirage-iOS/ (Xcode 工程)
├── MirageCore.xcframework     # 由 mirage-core + mirage-ios 编译 (staticlib)
├── MirageApp/
│   ├── MirageTunnelProvider.swift   # NEPacketTunnelProvider (建 TUN, 控内核)
│   ├── ContentView.swift             # UI: 节点管理 / 连接开关 / 状态
│   └── MirageBridge.swift            # C ABI 包装调用
```

mirage-core 的 iOS 包装 (`native/mirage-ios`):

```
src/lib.rs        # C ABI 导出 (extern "C", 无 JNI)
  mirage_start(int tun_fd, const char* uri, int pool_size) -> int
  mirage_stop()
  mirage_is_running() -> bool
  mirage_is_healthy() -> bool
  mirage_latency_ms() -> int64
  mirage_recent_logs(char** out, int* n)   # 或回调
src/device.rs     # iOS TUN 数据源适配 (NEPacketTunnelProvider)
```

## 2. TUN 数据源适配 (最关键的平台差异)

Android: VpnService 给一个 **fd**, mirage-core 用 `File::read/write` 读写 IP 包。
iOS: `NEPacketTunnelProvider` 没有 fd — 用 `packetFlow.readPackets / writePackets`:

```swift
// Swift 侧
override func startTunnel(options: [String: NSObject]?) throws {
    let settings = NETunnelNetworkSettings(tunnelRemoteAddress: "198.18.0.1")
    // 网络配置与 Android 完全一致:
    //   IPv4 198.18.0.1/32, 默认路由, DNS 198.19.0.53
    settings.ipv4Settings = NEIPv4Settings(addresses: ["198.18.0.1"], subnetMasks: ["255.255.255.255"])
    settings.ipv4Settings?.includedRoutes = [NEIPv4Route.default()]
    settings.dnsSettings = NEDNSSettings(servers: ["198.19.0.53"])
    try await applyTunnelNetworkSettings(settings)

    let fd = self.packetFlow.value(forKey: "socket") as! Int  // 私有 API? 见下方备注
    mirage_start(fd, uri, 4)
}
```

**推荐方案 (两条路)**:

### 方案 A: 拿 packetFlow 的 socket fd (简单, 但用私有 API)
`NEPacketTunnelProvider` 内部有一个 socket fd (`packetFlow.socket`)。KVC 取到后,
与 Android 的 TUN fd 语义完全一致 —— mirage-core 的 device 层零改动。缺点是依赖
私有 API (App Store 审核风险), 仅适用于自签/企业分发。

### 方案 B: 实现 iOS PacketTunnelDevice (推荐, 审核安全)
在 mirage-core 的 `tun::device` 层增加一个"外部泵"接口, 替代 fd 读线程:

```rust
// mirage-core/src/tun/device.rs (新增, 平台无关)
pub trait PacketSource: Send {
    /// 阻塞读一个 IP 包 (iOS 侧由 Swift 的 readPackets 回调填充)
    fn recv_packet(&mut self) -> std::io::Result<Vec<u8>>;
    fn send_packet(&mut self, pkt: &[u8]) -> std::io::Result<()>;
}

pub enum TunDeviceBackend {
    Fd(i32),                       // Android / 方案 A
    Source(Box<dyn PacketSource>)  // iOS 方案 B
}
```

Swift 侧实现 `PacketSource`:
```swift
class IosPacketSource {
    // mirage-core 泵线程阻塞等包; Swift 侧 readPackets 回调 push 进队列
    func start() {
        packetFlow.setReadHandler { packets, protocols in
            for p in packets { self.queue.push(p) }   // 唤醒 Rust 泵
        }
    }
}
```
> 注意: `NEPacketTunnelProvider.packetFlow.readPackets` 每包需要 `[NSNumber]` 协议
> 数组 (IPv4=2 / IPv6=30); `writePackets` 同样带协议号。

## 3. 构建流水线

```bash
# 1. 交叉编译 (需 macOS + Xcode)
rustup target add aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios
cd native/mirage-ios
cargo build --release --target aarch64-apple-ios   # 真机
cargo build --release --target aarch64-apple-ios-sim  # 模拟器

# 2. 打 xcframework (合并真机/模拟器)
xcodebuild -create-xcframework \
  -library target/aarch64-apple-ios/release/libmirage_ios.a \
  -library target/aarch64-apple-ios-sim/release/libmirage_ios.a \
  -output MirageCore.xcframework

# 3. Xcode 工程里链接 + 嵌入
#    ENABLE_BITCODE=NO (ring 不支持 bitcode)
```

CI 里也可以用同一个 `systemd-nspawn` 思路跑 macOS VM (或 GitHub Actions macos runner)。

## 4. 需要留意的小改动 (清单)

1. **`crate::protect`**: iOS 不需要, `set_protect_callback` 不调用即可 (已是 Option)。
2. **`pool.rs` 的 netlink 门控**: 已写 `#[cfg(not(any(target_os="android", target_os="ios")))]` ✓
3. **`brutal.rs` 的 SO_COOKIE**: Android 有补丁; iOS 无 SO_COOKIE 概念, 且移动端
   `brutal_rate_mbps=None` 不会走到。编译层面 `libc::SO_COOKIE` 在 iOS 目标是否缺失?
   → **移植第一步先跑 `cargo check --target aarch64-apple-ios`, 缺失就仿照 Android 补丁**。
4. **`tokio::signal` / `std::process::exit`**: mirage-core 不包含 CLI, 无此问题。
5. **`smoltcp` Medium::Ip + `set_any_ip`**: 与平台无关, 保持。
6. **DNS**: iOS 的 `NEDNSSettings` 把 DNS 指向 198.19.0.53 → App 查询走 TUN → 引擎应答。
   ⚠️ iOS 上 `NEVPNProtocolIPSec`/系统解析器可能缓存, 需要关闭 App 的 "Private Relay"
   或系统级代理冲突。
7. **前台保活**: iOS 用 `NEPacketTunnelProvider` 本身就是系统 VPN 上下文, 无额外保活。

## 5. 测试策略

- **单元测试**: `cargo test` (mirage-core 93 个测试, 平台无关)
- **端到端 (macOS 上模拟)**: 参考 `scripts/test-e2e.sh` — macOS 上可先建 utun (系统
  TUN) 跑同一套引擎验证, 再切 iOS 真机
- **真机**: Xcode 装到 iPhone, 配好开发者证书; 用同一服务端测试

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| ring 在 iOS 的编译 (asm/feature) | ring 官方支持 iOS; 若遇问题可切 `ring=0.17` 的 "wasm" 后端? 否 — 直接用官方 iOS 支持 |
| NEPacketTunnelProvider 无 fd | 方案 B (PacketSource trait) 是正路, 工作量可控 |
| App Store 审核 (VPN + 自研加密) | 需要申报 VPN 类目 + 加密出口合规 (ENC/SRC); 参考 WireGuard 开源 App 的做法 |
| 私有 API 依赖 | 方案 A 仅自签分发用 |
| 协议升级同步 | `native/vendor-sync.sh` 已就位, iOS 同样适用 |

## 7. 里程碑建议

1. M1: `cargo check --target aarch64-apple-ios` 全绿 (含 ring)
2. M2: macOS 上 utun + mirage-core e2e (复用 test-e2e.sh 思路)
3. M3: 空壳 iOS App + NEPacketTunnelProvider 跑通 (方案 B 的 PacketSource)
4. M4: 完整 UI + 节点管理 + 状态
5. M5: 真机验证 + 多服务端 + 长期稳定性
