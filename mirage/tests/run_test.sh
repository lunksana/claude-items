#!/bin/bash
# 跑吞吐基准：自动启动 server + client，跑 throughput_test.py，最后清理
# 在脚本位置 tests/ 下，运行时 cd 到项目根
set -euo pipefail
cd "$(dirname "$0")/.."

pkill -9 -f 'python3.*server.py' || true
pkill -9 -f 'python3.*client.py' || true
sleep 1

python3 -u server.py config_server.json > server.log 2>&1 &
SERVER_PID=$!
echo "Server starting (PID $SERVER_PID)..."

for i in {1..15}; do
    if grep -q "Listening on" server.log; then break; fi
    sleep 1
done

python3 -u client.py config_client.json > client.log 2>&1 &
CLIENT_PID=$!
echo "Client starting (PID $CLIENT_PID)..."

for i in {1..15}; do
    if grep -qiE "MIXED listening|SOCKS5 listening|listening on" client.log; then break; fi
    sleep 1
done

echo "Running throughput test..."
python3 -u tests/throughput_test.py

kill $CLIENT_PID
kill $SERVER_PID
