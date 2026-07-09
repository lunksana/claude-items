#!/usr/bin/env bash
# 针对低版本 Linux / glibc 缺失系统的静态构建脚本 (musl)
set -euo pipefail

cd "$(dirname "$0")"

echo "=== 1. 检查并添加 x86_64-unknown-linux-musl compilation target ==="
rustup target add x86_64-unknown-linux-musl

echo "=== 2. 开始编译 x86_64 musl 静态 release 版本 ==="
cargo build --release --target x86_64-unknown-linux-musl

mkdir -p dist
TARGET_BIN="target/x86_64-unknown-linux-musl/release/samba-webgui"
DIST_BIN="dist/samba-webgui-x86_64-musl"

cp "$TARGET_BIN" "$DIST_BIN"

# 进一步精简二进制大小（若存在 strip 且未在 Cargo.toml 完全裁剪）
if command -v strip >/dev/null 2>&1; then
    strip "$DIST_BIN" 2>/dev/null || true
fi

echo "=== 3. 编译与检查完成 ==="
echo "产物路径: $DIST_BIN"
if command -v file >/dev/null 2>&1; then
    file "$DIST_BIN"
fi
ls -lh "$DIST_BIN"

# 若安装了 cross 或 aarch64 musl 目标，提示/可选同时构建 ARM64 静态版
if rustup target list | grep -q "aarch64-unknown-linux-musl (installed)"; then
    echo ""
    echo "提示: 检测到您也安装了 aarch64-unknown-linux-musl，可执行:"
    echo "  cargo build --release --target aarch64-unknown-linux-musl"
fi
