# Mirage-Android

基于 [Mirage-rs](https://github.com/zdgt0226/Mirage-rs) 协议的 **Android 客户端** —— 移动端裁剪内核
(`mirage-core`, Rust) + 独立内核进程 + TUN 全流量代理, 现代化代理工具界面。

```
┌──────────────────────────────────────────────┐
│ App 进程 (Kotlin UI)                          │
│  首页/节点/规则/监控 + CoreController (AIDL)   │
├──────────────────────┬───────────────────────┤
│ AIDL Binder          │ 跨进程控制/状态         │
├──────────────────────▼───────────────────────┤
│ :core 进程 (CoreService: VpnService + JNI)   │
│  TUN 创建 + Rust 内核 (mirage-core)          │
└──────────────────────────────────────────────┘
```

## ✨ 功能特性

### 内核 (mirage-core, Rust)
- **Mirage 隧道协议** (vendored 自 Mirage-rs, 与 v0.9.2 兼容):
  TLS 1.3 ClientHello 字节级仿真 / ChaCha20-Poly1305 分帧 / PFS / cipher agility
- **TUN 全流量代理**: smoltcp 用户态协议栈, TCP + UDP
- **国内外分流**: 7727 段中国 IP + 国内域名白名单 + 用户自定义规则
  (Clash 匹配方式: DOMAIN-SUFFIX / DOMAIN / KEYWORD / REGEX / IP-CIDR)
- **fake-IP DNS**: 国内→真实 IP 直连, 国外→fake-IP 隧道, 抗污染
- **QUIC 屏蔽**: 海外 UDP 443 即时丢弃 → HTTP/2 降级
- **连接池优化**: 预热池 + on-demand 并发拨号 (信号量限流 8~16) + 连接回流

### 应用 (Kotlin)
- **独立内核进程** (:core), App 纯控制层, 崩溃隔离
- **节点管理**: mirage:// 链接/手动填写、多节点 CRUD、自动优选、批量测速+RTT 排序
- **分流规则**: Clash 匹配方式、内置规则查看 (GeoIP CN / GeoSite CN)、默认策略
- **监控**: 实时速率 (60s 曲线)、活跃连接列表、日志 (级别筛选/语法高亮)、
  流量统计 (会话 + **今日/本月持久化**)、规则命中统计
- **断线自动重连 / 节点 failover**: watchdog 检测 + 测活选优热切换
- **配置备份/恢复**: 节点+规则+设置一键 JSON 导入导出
- **通知栏实时流量**、前台服务 (Android 14/16 兼容)、16KB 对齐

## 状态

| 项 | 状态 |
|---|---|
| mirage-core (裁剪协议内核) | ✅ 编译通过, 97 单元测试 |
| 端到端 (TUN→隧道→真实网络) | ✅ nspawn 隔离容器 + 实机 (国内外分流验证通过) |
| mirage-jni (JNI 包装) | ✅ 交叉编译 arm64-v8a (16KB 对齐) |
| Android App (Kotlin) | ✅ 沙箱构建 APK, 实机运行正常 |
| Android 16 兼容 | ✅ 前台服务类型 + bindSocketToNetwork + 16KB 对齐 |
| iOS 移植 | 📋 规划中, 见 [docs/IOS_PORTING.md](docs/IOS_PORTING.md) |

## 快速开始 (Android)

```bash
# 1. 构建原生库 + APK (需 root; 内部用 systemd-nspawn 沙箱, 不动宿主配置)
scripts/build-android.sh

# 2. 产物
#    .build/out/app-debug.apk        (最新)
#    .build/out/mirage-v<版本>-<时间>.apk  (版本化归档)
```

安装后:
1. 打开 App → 「节点」Tab → 添加节点 (`mirage://密码@host:端口?sni=...`, 或手动填字段)
2. 首页点「连接」→ 系统 VPN 授权 → 允许
3. 首页实时速率/通知栏流量/监控 Tab 查看状态

## 目录结构

```
native/
  mirage-core/     # 平台无关 Rust 内核 (协议裁剪 + TUN 引擎 + 分流)
    src/vendor/    # 从 mirage-rs vendored 的协议代码 (native/vendor-sync.sh 同步)
    src/tun/       # smoltcp TUN 引擎 (device/tcp/udp/dns)
    src/direct.rs  # 国内外分流 (CN IP 段 + 域名白名单 + 自定义规则 + 命中统计)
    src/proxy/     # pool (on-demand 拨号/信号量/回流) / outbound / tunnel
    src/crypto/    # 加密与 TLS 仿真 (aead/hello_auth/tls_raw/pfs/handshake_cache)
  mirage-jni/      # Android JNI 包装 (cdylib, 16KB 对齐)
android/           # Kotlin App (Gradle, 单 Activity + 底部导航 + ViewModel)
  app/src/main/aidl/  # ICoreService (跨进程控制接口)
  app/src/main/java/.../core/   # CoreController / NodeStore / RuleStore / ConfigBackup / TrafficStatsStore
  app/src/main/java/.../ui/     # Home/Nodes/Rules/Traffic Fragment + ViewModel
scripts/
  build-android.sh # 一键构建 (native + APK, nspawn 沙箱, 版本化归档)
  test-e2e.sh      # 端到端验证 (systemd-nspawn 隔离)
  vendor-sync.sh   # 从 mirage-rs 同步协议代码
  debloat.sh       # 系统 bloat 精简工具 (可选)
docs/
  ARCHITECTURE.md    # 整体架构 (Android/iOS 通用视角)
  ARCHITECTURE_V2.md # 独立内核进程 + AIDL 架构
  ANDROID_BUILD.md   # Android 构建指南
  IOS_PORTING.md     # iOS 移植指南 (重要)
  PROTOCOL.md        # Mirage 协议客户端视角
  VERIFICATION.md    # 端到端验证记录
```

## 架构要点

- **独立内核进程** (:core): App 通过 AIDL (`ICoreService`) 控制内核 (注入节点/规则、开关、状态),
  崩溃隔离, UI 与内核解耦
- **TUN 数据面**: TUN fd → smoltcp → (fake-IP 反查/CN 段判定) → Mirage 隧道 → 服务端
- **节点热切换**: `setNode` 运行时重建引擎 (arc-swap), 无需断开
- **隧道 socket 保护**: protect + `bindSocketToNetwork` (非 VPN 网络), 防 TUN 环路
  (Android 16 关键)

## 文档

- [架构设计](docs/ARCHITECTURE.md)
- [**独立进程架构 v2**](docs/ARCHITECTURE_V2.md)
- [Android 构建指南](docs/ANDROID_BUILD.md)
- [**iOS 移植指南**](docs/IOS_PORTING.md)
- [协议说明 (客户端视角)](docs/PROTOCOL.md)
- [端到端验证记录](docs/VERIFICATION.md)

## 与 Mirage-rs 的关系

- 协议内核 (crypto / pool / outbound / tunnel / fake_ip / node_uri) 从 Mirage-rs **vendored** 进
  `native/mirage-core/src/vendor/`, 同步见 `native/vendor-sync.sh` (记录上游 commit)。
- 移动端**裁剪边界**: 去掉 eBPF / 透明网关 / Web 看板 / 配置热重载 / geo / WireGuard /
  Shadowsocks / splice 零拷贝 / brutal CC / netlink 链路自愈。
- 协议升级时: 跑 `native/vendor-sync.sh` → 按提示修裁剪边界 → 跑测试。

## 安全声明

继承自 Mirage-rs: 本项目**未经独立安全审计**, 加密分帧与握手认证为自研, 请勿用于
生命安全级场景。认证为单一共享口令; 前向保密 (PFS) 需两端同开。

## License

GPL-3.0-or-later (与 Mirage-rs 一致)。
