#!/bin/bash
# 同 run_test.sh，精简版
set -euo pipefail
cd "$(dirname "$0")/.."

pkill -9 -f 'python3.*server.py' || true
pkill -9 -f 'python3.*client.py' || true
sleep 1

python3 -u server.py config_server.json > server.log 2>&1 &
SERVER_PID=$!

for i in {1..15}; do
    if grep -q "Listening on" server.log; then break; fi
    sleep 1
done

python3 -u client.py config_client.json > client.log 2>&1 &
CLIENT_PID=$!

for i in {1..15}; do
    if grep -qiE "MIXED listening|SOCKS5 listening|listening on" client.log; then break; fi
    sleep 1
done

python3 -u tests/throughput_test.py

kill $CLIENT_PID
kill $SERVER_PID
