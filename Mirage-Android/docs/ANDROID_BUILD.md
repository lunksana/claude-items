# Android 构建指南

## 构建环境

推荐在 **systemd-nspawn 沙箱**内构建 (完全隔离, 不污染宿主):
- 宿主需 root + `systemd-nspawn` (`apt install systemd-container`)
- 容器: Ubuntu 22.04 minbase (已有 `android-builder` 镜像, 见 `/var/lib/machines/`)
- 工具链: JDK 17 + Gradle 8.9 + Android SDK (compileSdk 34) + NDK r26 + Rust (1.95+)

## 一键构建

```bash
scripts/build-android.sh        # native (Rust 交叉编译) + apk (沙箱 Gradle)
scripts/build-android.sh native # 只构建 .so
scripts/build-android.sh apk    # 只构建 APK (用已有 .so)
```

产物:
- `.build/out/app-debug.apk` (13MB)
- `android/app/src/main/jniLibs/{arm64-v8a,x86_64}/libmirage_jni.so`

## 分步说明

### 1. Rust 交叉编译 (宿主)

```bash
# rustup targets (一次)
rustup target add aarch64-linux-android x86_64-linux-android

# 设置 cargo 链接器 (NDK 真实 clang, 必须用带 sysroot 的版本):
# ~/.cargo/config.toml
# [target.aarch64-linux-android]
# linker = "/opt/android-sdk/ndk/<ver>/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android24-clang"
# ar = ".../llvm-ar"
# (同样设置 x86_64 / armv7)

cd native/mirage-jni
cargo build --release --target aarch64-linux-android --lib
cargo build --release --target x86_64-linux-android --lib
# 产物拷到 android/app/src/main/jniLibs/<abi>/
```

> ⚠️ ring/aws-lc-sys 等 C 依赖的 cc-rs 需要能发现 Android sysroot:
> 用 `CC_aarch64_linux_android` 等环境变量指向 NDK 的真实 clang (无版本号包装脚本会
> 找不到 sysroot)。见 `scripts/build-android.sh` 中的设置。

### 2. Gradle APK (沙箱)

```bash
systemd-nspawn -D /var/lib/machines/android-builder --as-pid2 \
  --bind=<repo>/android:/workspace \
  --bind=<repo>/.build/out:/output \
  --bind=<repo>/.build/gradle-home:/root/.gradle \
  --bind=/opt/android-sdk:/android-sdk \
  /bin/bash -c 'export JAVA_HOME=/opt/jdk-17 ANDROID_HOME=/android-sdk ...; cd /workspace && gradle assembleDebug'
```

## 设备安装与测试

1. `adb install .build/out/app-debug.apk`
2. 打开 App → 粘贴节点 (与服务端 install.sh 导出的 `mirage://` 串一致) → 保存 → 连接
3. 首次会弹 VPN 授权 (系统级, 不可跳过)
4. 验证: 状态栏 RTT / 日志面板 / 浏览器访问外网

## 常见问题

| 问题 | 处理 |
|---|---|
| `ring` C 编译找不到 sysroot | 用 NDK 真实 clang 路径, 见上 |
| Gradle 下载依赖慢 | 预置 `.build/gradle-home` 缓存 (bind 进容器) |
| VPN 授权反复弹 | 到系统设置里撤销/重授 Mirage 的 VPN 权限 |
| 连不上服务端 | 检查节点 URI 的 host 可达性 + 服务端端口/密码/SNI 一致 |

## 打正式包 (release)

```bash
cd android && ./gradlew assembleRelease
# 需要签名配置 (android/app/build.gradle.kts 的 signingConfigs)
```
