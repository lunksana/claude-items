#!/usr/bin/env bash
# 针对低版本 Linux / glibc 缺失系统的静态构建脚本 (musl)
# 产物为不依赖任何系统动态库的单文件二进制，放置于 dist/。
# 默认构建 x86_64；可用 TARGET=aarch64-unknown-linux-musl ./build-musl.sh 构建 ARM64。
set -euo pipefail

cd "$(dirname "$0")"

TARGET="${TARGET:-x86_64-unknown-linux-musl}"
ARCH_TAG="${TARGET%%-*}"

echo "=== 1. 检查并添加 ${TARGET} 编译目标 ==="
rustup target add "${TARGET}"

echo "=== 2. 编译 ${TARGET} 静态 release 版本 ==="
cargo build --release --target "${TARGET}"

mkdir -p dist
TARGET_BIN="target/${TARGET}/release/samba-webgui"
DIST_BIN="dist/samba-webgui-${ARCH_TAG}-musl"

cp "${TARGET_BIN}" "${DIST_BIN}"

# release profile 已 strip；此处兜底再 strip 一次（跨架构 strip 可能失败，忽略即可）
command -v strip >/dev/null 2>&1 && strip "${DIST_BIN}" 2>/dev/null || true

echo "=== 3. 校验静态链接 ==="
command -v file >/dev/null 2>&1 && file "${DIST_BIN}"
if command -v ldd >/dev/null 2>&1; then
    if ldd "${DIST_BIN}" 2>&1 | grep -qiE "not a dynamic executable|statically linked"; then
        echo "✔ 静态链接校验通过（无动态库依赖）"
    else
        echo "✗ 警告：该二进制似乎仍有动态依赖：" >&2
        ldd "${DIST_BIN}" >&2 || true
        exit 1
    fi
fi

# 生成 sha256 校验值，方便分发核对
if command -v sha256sum >/dev/null 2>&1; then
    ( cd dist && sha256sum "$(basename "${DIST_BIN}")" | tee "$(basename "${DIST_BIN}").sha256" )
fi

echo ""
echo "=== 完成 ==="
ls -lh "${DIST_BIN}"
echo "产物: ${DIST_BIN}"

if [ "${TARGET}" != "aarch64-unknown-linux-musl" ] && rustup target list | grep -q "aarch64-unknown-linux-musl (installed)"; then
    echo ""
    echo "提示: 已安装 aarch64-unknown-linux-musl，可执行 ARM64 静态构建:"
    echo "  TARGET=aarch64-unknown-linux-musl ./build-musl.sh"
fi
