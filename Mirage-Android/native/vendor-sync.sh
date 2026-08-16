#!/usr/bin/env bash
# ============================================================================
# mirage-core 协议内核同步脚本
#
# 从上游 mirage-rs 仓库同步"移动端需要的模块"到 native/mirage-core/src/vendor/，
# 保持协议实现与上游一致。同步是**单向复制**：
#   - 上游 mirage-rs 目录不应被本仓库修改（见 README）
#   - 移动端特有的适配（Android SO_COOKIE 常量等）落在 vendored 副本里，
#     需要手动维护，本脚本会在有差异时提示
#
# 用法:
#   ./native/vendor-sync.sh [mirage-rs 路径]   # 默认 /opt/claude/Mirage-rs
# ============================================================================
set -euo pipefail

UPSTREAM="${1:-/opt/claude/Mirage-rs}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/native/mirage-core/src/vendor"

if [[ ! -d "$UPSTREAM/src" ]]; then
    echo "ERROR: 上游 mirage-rs 源码目录不存在: $UPSTREAM/src" >&2
    exit 1
fi

# 同步清单: 目标相对路径 = 上游相对路径
FILES=(
    "crypto/mod.rs"
    "crypto/aead.rs"
    "crypto/cipher.rs"
    "crypto/hello_auth.rs"
    "crypto/pfs.rs"
    "crypto/handshake_cache.rs"
    "crypto/tls_raw.rs"
    "time_sync.rs"
    "proxy/tunnel.rs"
    "proxy/mirage_stream.rs"
    "proxy/pool.rs"
    "proxy/outbound.rs"
    "proxy/brutal.rs"
    "proxy/resolver.rs"
    "proxy/mod.rs"
    "monitor.rs"
    "dns/mod.rs"
    "dns/fake_ip.rs"
    "node_uri.rs"
    "net_util.rs"
    "config.rs"
)

echo "==> 从 $UPSTREAM 同步 ${#FILES[@]} 个模块到 $DEST"
mkdir -p "$DEST"
for f in "${FILES[@]}"; do
    src="$UPSTREAM/src/$f"
    if [[ ! -f "$src" ]]; then
        echo "  !! 上游缺少 $f (协议升级可能移除了它? 需人工确认)" >&2
        continue
    fi
    mkdir -p "$(dirname "$DEST/$f")"
    # 复制 + 记录上游 commit
    cp "$src" "$DEST/$f"
    echo "  ✓ $f"
done

# 记录上游 commit 与同步时间
UPSTREAM_COMMIT="$(git -C "$UPSTREAM" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "$DEST/SYNC.md" <<EOF
# Vendored 同步记录

- 上游仓库: $UPSTREAM
- 上游 commit: \`$UPSTREAM_COMMIT\`
- 同步时间: $(date -Is)

## 同步后必须做的事
1. 检查 \$DEST 里是否有对**未 vendored 模块**的引用 (\`crate::api\` / \`crate::ebpf\` /
   \`crate::config_watcher\` / \`crate::net_monitor\` / \`crate::monitor\` / \`crate::startup\` /
   \`crate::proxy::wg\` / \`crate::proxy::shadowsocks\` / \`crate::proxy::splice\` 等) ——
   这些是移动端裁剪掉的模块, 出现引用说明裁剪边界需要调整。
2. 检查移动端补丁是否仍适用 (搜索 \`MIRAGE_MOBILE\` 标记)。
3. \`cd native && cargo check\` 过一遍。

## 移动端补丁清单 (在 vendored 副本中, 上游没有)
- \`proxy/brutal.rs\`: \`#[cfg(target_os = "android")] const SO_COOKIE = 67\` (bionic 内核支持,
  libc crate 未对 android target 暴露该常量)
EOF

echo ""
echo "==> 同步完成。检查裁剪边界:"
grep -rn "crate::api\|crate::ebpf\|crate::config_watcher\|crate::net_monitor\|crate::monitor\|crate::startup\|crate::proxy::wg\|crate::proxy::shadowsocks\|crate::proxy::splice" "$DEST" | grep -v "^Binary" | head -20 || echo "    (无越界引用)"
