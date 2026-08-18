#!/usr/bin/env bash
# ============================================================================
# Mirage-Android 一键构建: systemd-nspawn 沙箱内完成 Rust 交叉编译 + Gradle APK
#
# 流程:
#   1. 宿主: cargo-ndk 交叉编译 mirage-jni (arm64-v8a + x86_64) → jniLibs
#   2. 宿主: 把 android 源码 bind 进 android-builder 容器
#   3. 容器内: gradle assembleDebug → APK 输出到宿主
#
# 用法:
#   scripts/build-android.sh          # 完整构建
#   scripts/build-android.sh native   # 只构建 Rust 原生库
#   scripts/build-android.sh apk      # 只构建 APK (用已有 jniLibs)
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="android-builder"
NDK="/opt/android-sdk/ndk/26.3.11579264"
CARGO="${CARGO:-cargo}"

cmd="${1:-all}"

build_native() {
    echo "==> [1/2] 交叉编译 mirage-jni (arm64-v8a) ..."
    export ANDROID_NDK_HOME="$NDK"
    export PATH="/opt/android-toolchain/bin:$PATH"
    local out="$HERE/android/app/src/main/jniLibs"
    rm -rf "$out"; mkdir -p "$out"

    cd "$HERE/native/mirage-jni"
    # 用宿主 rustup + NDK 直接构建 (16KB 页对齐 link-arg 由 .cargo/config.toml 统一声明)
    for target in aarch64-linux-android; do
        local abi="arm64-v8a"
        echo "  -- $target ($abi) [16KB Page Aligned]"
        cargo build --release --target "$target" --lib
        mkdir -p "$out/$abi"
        cp "$HERE/native/mirage-jni/target/$target/release/libmirage_jni.so" "$out/$abi/"
    done
    echo "  ✓ jniLibs:"
    find "$out" -name "*.so" -exec ls -lh {} \; | awk '{print "    " $NF " (" $5 ")"}'
}

build_apk() {
    echo "==> [2/2] 沙箱内 Gradle 构建 APK ..."
    # 确认容器存在
    if [[ ! -d "/var/lib/machines/$CONTAINER" ]]; then
        echo "!! 容器 $CONTAINER 不存在, 请先运行 scripts/setup-sandbox.sh" >&2
        exit 1
    fi
    # 输出/缓存目录
    mkdir -p "$HERE/.build/out" "$HERE/.build/gradle-home"
    chown 1000:1000 "$HERE/.build/out" "$HERE/.build/gradle-home" 2>/dev/null || true

    local ver
    ver=$(grep -E 'versionName\s*=' "$HERE/android/app/build.gradle.kts" | head -1 | sed -E 's/.*"([^"]+)".*/\1/' || echo "0.2.0")
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)

    # 容器内以 root 构建 (SDK bind 自宿主, 避免容器内再下载)
    systemd-nspawn -D "/var/lib/machines/$CONTAINER" --as-pid2 \
        --bind="$HERE/android:/workspace" \
        --bind="$HERE/.build/out:/output" \
        --bind="$HERE/.build/gradle-home:/root/.gradle" \
        --bind="/opt/android-sdk:/android-sdk" \
        /bin/bash -c '
            set -e
            export JAVA_HOME=/opt/jdk-17
            export ANDROID_HOME=/android-sdk
            export ANDROID_NDK_HOME=/android-sdk/ndk/26.3.11579264
            export GRADLE_USER_HOME=/root/.gradle
            export GRADLE_OPTS="-Dorg.gradle.native=false"
            export PATH=/opt/jdk-17/bin:/opt/gradle-8.9/bin:$PATH
            cd /workspace
            gradle assembleDebug --no-daemon
            cp app/build/outputs/apk/debug/app-debug.apk /output/latest-build.apk 2>/dev/null || true
        ' 2>&1 | tail -25

    local raw_apk="$HERE/.build/out/latest-build.apk"
    if [[ -f "$raw_apk" ]]; then
        local versioned_apk="$HERE/.build/out/mirage-v${ver}-${timestamp}.apk"
        local release_name_apk="$HERE/.build/out/mirage-v${ver}-debug.apk"
        local default_apk="$HERE/.build/out/app-debug.apk"

        # 保留带时间戳与版本号的历史存档，不覆盖之前版本
        cp "$raw_apk" "$versioned_apk"
        cp "$raw_apk" "$release_name_apk"
        mv "$raw_apk" "$default_apk"

        echo ""
        echo "  ✓ 本次编译产物: $versioned_apk ($(du -h "$versioned_apk" | cut -f1))"
        echo "  ✓ 最新指向软链: $default_apk"
        echo ""
        echo "==> [.build/out/] 历史已归档的所有版本 APK 列表:"
        ls -lh "$HERE/.build/out"/*.apk 2>/dev/null | awk '{print "    " $NF " (" $5 ", " $6 " " $7 " " $8 ")"}'
        echo ""
    else
        echo "!! APK 未生成 (见上方日志)" >&2
        exit 1
    fi
}

case "$cmd" in
    native) build_native ;;
    apk) build_apk ;;
    all) build_native; build_apk ;;
    *) echo "用法: $0 {all|native|apk}"; exit 1 ;;
esac
