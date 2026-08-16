# Mirage-Android 架构设计

> Android 与 iOS 共享同一份平台无关 Rust 内核 (`mirage-core`), 差异仅在平台包装层
> (Android = JNI cdylib, iOS = Swift FFI/staticlib)。本文件从**双平台通用**视角描述。

## 1. 总体分层

```
┌────────────────────────────────────────────────────────────────┐
│ 平台层 (每平台一份)                                              │
│   Android: Kotlin App (UI) + VpnService + MirageNative (JNI)   │
│   iOS:     SwiftUI App + NEPacketTunnelProvider + mirage-ios   │
│     - 提供 TUN fd / packet tunnel fd                            │
│     - 提供 socket 保护 (Android protect / iOS 无需)             │
│     - UI / 节点管理 / 状态显示 / 日志                            │
├────────────────────────────────────────────────────────────────┤
│ 包装层                                                        │
│   Android: mirage-jni (cdylib, JNI 导出)                      │
│   iOS:     mirage-ios (staticlib, C ABI / swift-bridge)       │
│     - 持有专用 tokio runtime (独立线程, 不占 UI 线程)           │
│     - start/stop/status 控制                                    │
├────────────────────────────────────────────────────────────────┤
│ 内核层: mirage-core (平台无关, 无 JNI/无 UI)                  │
│   ├─ vendored 协议: crypto / proxy::pool / outbound /          │
│   │   tunnel / mirage_stream / dns::fake_ip / node_uri         │
│   │   (与 Mirage-rs 上游协议逐字节兼容)                        │
│   ├─ tun 引擎: smoltcp 用户态协议栈 + TUN 数据面               │
│   └─ engine: 出站管理 + fake-IP 映射                           │
└────────────────────────────────────────────────────────────────┘
```

## 2. 数据面 (一条 TCP 连接的一生)

```
App 发 SYN (目标 = 真实域名解析出的 fake-IP, 如 198.18.0.2)
  │ 内核路由: 0.0.0.0/0 → tun0
  ▼
TUN fd ──▶ [读线程] ──▶ [泵任务] handle_rx_packet
  │
  ├─ UDP 包 → parse_udp_datagram 直接数据报路径 (绕开 smoltcp)
  │    ├─ DNS (dst=198.19.0.53:53) → tun::dns 应答 fake-IP, 直接构 IP 包回
  │    └─ 其他 → tun::udp 按 (client,dst) 建流 → Mirage UDP 隧道
  │
  └─ TCP SYN → prescan_tcp: 建 smoltcp catcher socket (listen(None, dst_port))
       │   (smoltcp set_any_ip(true): 接受发往任意地址的 SYN)
       ▼
    泵 poll → smoltcp 完成三次握手 (SYN-ACK 源 = SYN 的目的地址)
       │
       ▼
    relay_tcp 任务: 等 Established → 读 socket 目的 (fake-IP)
       │
       ├─ fake_ip_reverse(fake-IP) → 域名 (如 www.google.com)
       │
       ▼
    OutboundNode::connect(Address::Domain(域名, 端口))
       │  目标头 [2B len][host:port] 发进隧道, 服务端远程解析 (抗污染)
       ▼
    WarmPool 隧道 (加密分帧 ChaCha20-Poly1305 / AES-256-GCM) → 服务端 → 真实目标
       │
       ▼
    双向转发: 上行(读 smoltcp socket → send_data) / 下行(recv_data → 写 socket)
```

UDP 数据面 (QUIC/游戏):
```
UDP 包 → 直接解析 (不经 smoltcp, 见 tun/udp.rs) → 流表按 (client,dst) 建流
  → 每条流一条 Mirage UDP 隧道 (首帧 [0x00] 哨兵) → 帧 [2B len][ATYP][ADDR][PORT][payload]
  → 回程: 解帧 → 手工构造 IP 包 (伪源 = 原目标) → 写 TUN
```
> 为什么不直接用 smoltcp 的 UdpSocket: smoltcp 按 (dst_addr, dst_port) 分发且首个匹配
> 即交付, 无法把**同一目的的多客户端**数据报分开回程; 直接数据报路径与上游
> `transparent_udp.rs` 的 per-flow 设计一致, 且免去 smoltcp UDP 缓冲管理。

## 3. 关键设计决策

### 3.1 为什么 vendored 而非依赖上游 crate
- 用户要求 Mirage-rs 仓库保持原状 (不在其目录编译/修改)
- 移动端裁剪边界 (去掉 eBPF/看板/WG/SS/brutal/netlink) 需要改模块图
- `native/vendor-sync.sh` 单向同步 + 记录 commit, 升级协议时有明确的核对清单

### 3.2 TUN 地址规划 (App 与内核必须严格一致)
| 项 | 值 | 说明 |
|---|---|---|
| 接口地址 | 198.18.0.1/32 | 点对点, /32 |
| fake-IP 段 | 198.18.0.0/16 | 映射器顺序分配 (198.18.0.2 起) |
| **DNS 地址** | **198.19.0.53** | ⚠️ 不能是接口地址 (内核当本地地址直接投递, 包不进 TUN) |
| 路由 | 0.0.0.0/0 + 198.18.0.0/15 | 全流量进 TUN |
| MTU | 1500 | |

### 3.3 smoltcp 透明代理三件套 (移植到 iOS 时必须保持)
1. **`set_any_ip(true)`** — 接受发往任意地址的包, 允许以任意源地址应答
2. **catcher 惰性建 socket** — SYN 预扫描, 按 4 元组建 `listen(None, port)`;
   每个连接一条 socket, 天然支持并发; 重传 SYN 靠 4 元组匹配去重
3. **默认路由 via 自己** — `add_default_ipv4_route(peer)`, 让非本机地址包过路由检查

### 3.4 隧道 socket 保护 (仅 Android)
Android `VpnService` 要求代理自己建立的出站 socket 必须 `protect(fd)`, 否则隧道流量
会重新路由进 TUN 造成环路:
```
mirage-core proxy::pool 建连后 → protect::protect(fd) → 队列
Kotlin 每 200ms drainProtectFds() → vpnService.protect(fd)
```
iOS `NEPacketTunnelProvider` 无此要求 (隧道 socket 默认走真实网络), 移植时跳过即可。

### 3.5 线程模型
- 专用 tokio runtime (2 worker, 独立线程, `thread_name="mirage-core"`)
- 泵任务: 唯一拥有 smoltcp `Interface` 的协程 (rx 包 / 25ms tick / 唤醒)
- 每个 TCP 连接一个 relay 任务; 每个 UDP 流一个 relay 任务
- smoltcp socket 访问都在同一把 `Mutex<TunInner>` 下, **持锁时禁止 await**
  (与上游 WG 模块同一约束)

## 4. 裁剪边界 (mirage-core vs mirage-rs)

| 模块 | 处理 |
|---|---|
| crypto (全部) | ✅ vendored 保留 |
| proxy/pool (WarmPool) | ✅ 保留; netlink 链路自愈裁剪 (移动端用 stale 探测兜底) |
| proxy/outbound | ⚠️ 裁剪: 仅 Mirage/Direct/Block + 组; WG/SS 变体移除 |
| proxy/tunnel, mirage_stream | ✅ 保留 |
| dns/fake_ip | ✅ 保留 (/16 而非上游 /15, 给 DNS 地址留 198.19.0.53) |
| node_uri, net_util, time_sync, monitor | ✅ 保留 |
| proxy/brutal | ⚠️ Android: SO_COOKIE 常量补丁 (libc crate 未暴露) |
| eBPF / api(看板) / config_watcher / geo / transparent* / splice | ❌ 移除 |
| wg / shadowsocks / ss_inbound / udp_mux / mixed / healthcheck | ❌ 移除 |
| net_monitor (netlink) | ❌ 移除 (见 pool.rs 的 cfg 门控) |

## 5. iOS 移植要点 (详见 IOS_PORTING.md)

1. `mirage-core` 直接编译为 iOS staticlib (aarch64-apple-ios, 无 JNI 依赖)
2. TUN fd 来源: `NEPacketTunnelProvider.packetFlow` (读/写 IP 包), 包一层
   `TunDevice` 适配, 替换现在的 `std::fs::File` 读线程 + 写 fd 路径
3. 无 `VpnService.protect` 需求 → protect hook 直接不注册
4. 无 netlink; `cfg(not(any(target_os="android", target_os="ios")))` 门控已就位
