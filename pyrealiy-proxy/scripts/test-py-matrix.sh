#!/usr/bin/env bash
# 跨 Python 版本兼容性冒烟测试
#
# 在 3.9 / 3.10 / 3.11 / 3.12 / 3.13 上分别建独立 venv、装依赖、
# 对全部 .py 模块做 import 测试。任何版本失败立即用红色提示。
#
# 用法：
#   bash scripts/test-py-matrix.sh                  # 跑全部默认版本
#   PY_VERSIONS="3.10 3.12" bash scripts/test-py-matrix.sh   # 指定子集
#
# 前置：
#   curl -LsSf https://astral.sh/uv/install.sh | sh   # 装 uv

set -euo pipefail

cd "$(dirname "$0")/.."

VENVS_DIR="${VENVS_DIR:-.venvs}"
read -r -a VERSIONS <<< "${PY_VERSIONS:-3.9 3.10 3.11 3.12 3.13}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
    red "未找到 uv。请先安装："
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 2
fi

echo "── 确保 Python 版本已装 ──"
uv python install "${VERSIONS[@]}" 2>&1 | sed 's/^/  /' || true

failed=()
passed=()

for py in "${VERSIONS[@]}"; do
    venv="$VENVS_DIR/py$py"
    echo
    echo "──────────── Python $py ────────────"

    uv venv --python "$py" "$venv" -q --clear
    dim "  venv: $venv"

    uv pip install --python "$venv/bin/python" -q cryptography

    if "$venv/bin/python" -c '
import sys
print(f"  running on {sys.version.split()[0]}")

modules = [
    "core.bloom", "core.brutal", "core.camouflage",
    "core.conn_pool", "core.dns_forwarder", "core.geosite_cache",
    "core.handshake_cache", "core.hello_auth", "core.router",
    "core.sniffer", "core.socks5", "core.stats", "core.tls_raw",
    "core.tunnel", "core.utils", "core.admin",
    "client", "server", "setup",
]
for m in modules:
    __import__(m)
print(f"  {len(modules)} modules imported OK")
'; then
        green "  ✓ Python $py"
        passed+=("$py")
    else
        red "  ✗ Python $py FAILED"
        failed+=("$py")
    fi
done

echo
echo "════════════════════════════════════════"
green "✓ passed: ${passed[*]:-(none)}"
if [ "${#failed[@]}" -gt 0 ]; then
    red "✗ failed: ${failed[*]}"
    exit 1
fi
green "All Python versions compatible."
