# Mirage-Android

基于 [Mirage-rs](https://github.com/zdgt0226/Mirage-rs) 协议的 **Android 客户端** —— 移动端裁剪内核
(`mirage-core`, Rust) + TUN 全流量代理 + 原生 VPNService App。

```
手机 App (Kotlin: UI + VpnService)
   │ JNI
   ▼
mirage-jni (cdylib, aarch64-linux-android)
   │
   ▼
mirage-core (平台无关 Rust 内核)
   ├── vendored 协议内核 (crypto / pool / outbound / tunnel / fake_ip / node_uri)
   │      └─ 与 Mirage-rs v0.9.2 协议逐字节兼容: TLS1.3 ClientHello 仿真握手 /
   │         ChaCha20-Poly1305 分帧 / PFS / cipher agility
   └── tun 引擎 (smoltcp 用户态协议栈) — TUN → smoltcp → Mirage 隧道 → 服务端
```

## 状态

| 项 | 状态 |
|---|---|
| mirage-core (裁剪协议内核) | ✅ 编译通过, 93 单元测试 |
| 端到端 (TUN→隧道→真实网络) | ✅ 在 systemd-nspawn 隔离容器验证: google/cloudflare/baidu 全 200 |
| mirage-jni (Android JNI 包装) | ✅ 交叉编译 arm64-v8a + x86_64 |
| Android App (Kotlin) | ✅ 沙箱内 Gradle 构建出 APK (13MB) |
| iOS 移植 | 📋 规划中, 见 [docs/IOS_PORTING.md](docs/IOS_PORTING.md) |

## 快速开始 (Android)

```bash
# 1. 构建原生库 + APK (需 root; 内部用 systemd-nspawn 沙箱, 不动宿主配置)
scripts/build-android.sh

# 2. 产物
#    .build/out/app-debug.apk
#    android/app/src/main/jniLibs/{arm64-v8a,x86_64}/libmirage_jni.so
```

安装 APK 后:
1. 打开 App, 粘贴节点 `mirage://密码@host:端口?sni=www.apple.com` (与服务端 install.sh 导出的串一致)
2. 保存 → 点「连接」→ 系统弹 VPN 授权 → 允许
3. 全流量 (TCP+UDP) 经 Mirage 隧道转发; 状态栏显示 RTT

## 目录结构

```
native/
  mirage-core/     # 平台无关 Rust 内核 (协议裁剪 + TUN 引擎)
    src/vendor/    # 从 mirage-rs vendored 的协议代码 (同步脚本: native/vendor-sync.sh)
    src/tun/       # smoltcp TUN 引擎 (device/stack/tcp/udp/dns)
    src/engine.rs  # 移动端引擎 (出站管理 + fake-IP)
    src/proxy/     # 裁剪后的 pool/outbound/tunnel/mirage_stream
    src/crypto/    # vendored 加密与 TLS 仿真 (aead/hello_auth/tls_raw/pfs)
  mirage-jni/      # Android JNI 包装 (cdylib)
android/           # Kotlin App (Gradle)
scripts/
  build-android.sh # 一键构建 (native + APK, nspawn 沙箱)
  test-e2e.sh      # 端到端验证 (systemd-nspawn 隔离, 验证 DNS fake-IP + HTTPS 全链路)
  vendor-sync.sh   # 从 mirage-rs 同步协议代码
docs/
  ARCHITECTURE.md  # 整体架构 (Android/iOS 通用视角)
  ANDROID_BUILD.md # Android 构建指南
  IOS_PORTING.md   # iOS 移植指南 (重要)
  PROTOCOL.md      # Mirage 协议客户端视角
  VERIFICATION.md  # 端到端验证记录
```

## 文档

- [架构设计](docs/ARCHITECTURE.md)
- [Android 构建指南](docs/ANDROID_BUILD.md)
- [**iOS 移植指南**](docs/IOS_PORTING.md)
- [协议说明 (客户端视角)](docs/PROTOCOL.md)
- [端到端验证记录](docs/VERIFICATION.md)

## 与 Mirage-rs 的关系

- 协议内核 (crypto / pool / outbound / tunnel / fake_ip / node_uri) 从 Mirage-rs **vendored** 进
  `native/mirage-core/src/vendor/`, 同步见 `native/vendor-sync.sh` (记录上游 commit)。
- 移动端**裁剪边界**: 去掉 eBPF / 透明网关 / Web 看板 / 配置热重载 / geo / WireGuard /
  Shadowsocks / splice 零拷贝 / brutal CC (Android 内核无模块) / netlink 链路自愈。
- 协议升级时: 跑 `native/vendor-sync.sh` → 按提示修裁剪边界 → 跑测试。

## 安全声明

继承自 Mirage-rs: 本项目**未经独立安全审计**, 加密分帧与握手认证为自研, 请勿用于
生命安全级场景。认证为单一共享口令; 前向保密 (PFS) 需两端同开。

## License

GPL-3.0-or-later (与 Mirage-rs 一致)。
