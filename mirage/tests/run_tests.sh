#!/bin/bash
# 回归测试入口。
#
#   ./tests/run_tests.sh          仅单元测试（无网络依赖，CI 友好，秒级）
#   ./tests/run_tests.sh --smoke  单元测试 + 端到端冒烟（需联网抓 camouflage TLS）
#
# 单元测试覆盖 router / config 的回归点（route.default、rule_set 映射、空 geo
# 存活、dns 投影、schema 白名单等）。smoke 真起 server+client 验证加密隧道数据路径。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "═══ 单元测试 (unittest) ═══"
python3 -m unittest discover -s tests -p 'test_*.py' -v

if [[ "${1:-}" == "--smoke" ]]; then
    echo
    echo "═══ 端到端冒烟 (smoke_e2e) ═══"
    python3 tests/smoke_e2e.py
fi

echo
echo "全部通过 ✓"
