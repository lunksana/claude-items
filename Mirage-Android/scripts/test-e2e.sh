#!/usr/bin/env bash
# ============================================================================
# 在 systemd-nspawn 容器内做 Mirage TUN 引擎端到端测试 (完全隔离, 不动宿主配置)
#
# 拓扑:
#   [宿主] ve-mirage-* (10.88.0.1/24) ──veth── host0 (10.88.0.2/24) [容器]
#        ├─ lite-server (0.0.0.0:8443, 宿主跑, 不被 TUN 卷进去)
#        └─ NAT → 外网 (仅容器出网用)
#   容器内: mirage0 (198.18.0.1/32) ──TUN引擎──▶ lite-server(10.88.0.1:8443)
#        └─ 默认路由走 TUN, DNS=198.19.0.53 (fake-IP)
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOTFS="/var/lib/machines/mirage-test-runner"
NS_PREFIX="ve-mirage"
NET="10.88.0"
PORT="8443"
PASS="test1234"

HOST_VETH=""
ORIG_FORWARD="$(cat /proc/sys/net/ipv4/ip_forward)"

cleanup() {
    pkill -9 -f "nspawn.*mirage-test-runner" 2>/dev/null || true
    if [[ -n "$HOST_VETH" ]]; then
        ip link del "$HOST_VETH" 2>/dev/null || true
    fi
    if [[ -f "$HERE/.build/server.pid" ]]; then
        kill "$(cat "$HERE/.build/server.pid")" 2>/dev/null || true
    fi
    echo "$ORIG_FORWARD" > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
    echo "==> 已清理 (ip_forward 已恢复为 $ORIG_FORWARD)"
}
trap cleanup EXIT

# 1. 容器内测试脚本
cat > "$ROOTFS/root/run-test.sh" <<'EOS'
#!/usr/bin/env bash
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
cd /root

echo "=== 容器内环境 ==="
ip link set host0 up 2>/dev/null
ip addr add 10.88.0.2/24 dev host0 2>/dev/null || true
ip link set lo up
ip route add default via 10.88.0.1 dev host0 2>/dev/null || true
echo "host0: $(ip -4 addr show host0 | grep inet | awk '{print $2}')"

echo "=== 起 TUN 引擎 (server 在宿主 10.88.0.1:8443) ==="
RUST_LOG=mirage_core=debug /opt/examples/tun_e2e --server 10.88.0.1 --port 8443 --password test1234 > /root/tun_e2e.log 2>&1 &
TUN_PID=$!
sleep 2

echo "=== 配置 TUN 接口与路由 ==="
ip addr add 198.18.0.1/32 dev mirage0 2>/dev/null || true
ip link set mirage0 up
ip route add 10.88.0.0/24 dev host0 2>/dev/null || true
ip route add default dev mirage0 2>/dev/null || true
ip route add 198.18.0.0/15 dev mirage0 2>/dev/null || true
echo "路由表:"; ip route

echo "=== DNS (fake-IP) ==="
echo "nameserver 198.19.0.53" > /etc/resolv.conf
# rp_filter 可能丢弃从 TUN 进、源=198.19.x 的回程包, 关掉验证
sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null
sysctl -w net.ipv4.conf.mirage0.rp_filter=0 2>/dev/null
sysctl -w net.ipv4.conf.default.rp_filter=0 2>/dev/null
echo "--- getent (fake-IP DNS) ---"
timeout 8 getent hosts www.google.com 2>&1 | head -2
echo "getent rc=$?"

echo "=== HTTPS 经隧道 ==="
timeout 25 curl -s -o /dev/null -w "google=%{http_code}\n" --connect-timeout 12 https://www.google.com 2>&1 || echo "curl google FAIL"
timeout 25 curl -s -o /dev/null -w "cloudflare=%{http_code}\n" --connect-timeout 12 https://www.cloudflare.com 2>&1 || echo "curl cf FAIL"
timeout 25 curl -s -o /dev/null -w "baidu=%{http_code}\n" --connect-timeout 12 https://www.baidu.com 2>&1 || echo "curl baidu FAIL"
timeout 25 curl -s -o /dev/null -w "cloudflare=%{http_code}\n" --connect-timeout 12 https://www.cloudflare.com 2>&1 || echo "curl cf FAIL"

echo "=== 引擎日志 (连接/DNS) ==="
tr -d '\000' < /root/tun_e2e.log | grep -aE "TUN-|ERROR" | tail -20

kill $TUN_PID 2>/dev/null
echo "=== 完成 ==="
EOS
chmod +x "$ROOTFS/root/run-test.sh"

# 2. 启动容器 (后台)
echo "==> 启动 nspawn 容器 ..."
systemd-nspawn -D "$ROOTFS" --as-pid2 --network-veth \
  --bind /dev/net/tun \
  --bind "$HERE/native/mirage-core/target/debug/examples:/opt/examples" \
  /root/run-test.sh > "$HERE/.build/nspawn-test.log" 2>&1 &
NSPAWN_PID=$!

# 3. 宿主侧 veth + NAT
for i in $(seq 1 20); do
    HOST_VETH=$(ip link | grep -oE "${NS_PREFIX}-[^:@]*" | head -1 || true)
    [[ -n "$HOST_VETH" ]] && break
    sleep 0.5
done
if [[ -z "$HOST_VETH" ]]; then
    echo "!! 未发现宿主 veth 接口"; exit 1
fi
ip addr add "$NET.1/24" dev "$HOST_VETH" 2>/dev/null || true
ip link set "$HOST_VETH" up
echo 1 > /proc/sys/net/ipv4/ip_forward
OUT_IF="$(ip route | awk '/^default/ {print $5; exit}')"
iptables -t nat -C POSTROUTING -s "$NET.0/24" -o "$OUT_IF" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s "$NET.0/24" -o "$OUT_IF" -j MASQUERADE 2>/dev/null || true
echo "==> 宿主 veth $HOST_VETH 已配置 ($NET.1)"

# 4. 宿主侧起 lite-server
cat > "$HERE/.build/lite_server.json" <<EOF
{
  "listen": "0.0.0.0",
  "port": $PORT,
  "password": "$PASS",
  "sni": "www.apple.com",
  "log_level": "info"
}
EOF
"$HERE/.build/mirage-rs-server-bin/mirage-rs" lite-server -c "$HERE/.build/lite_server.json" > "$HERE/.build/server.log" 2>&1 &
echo $! > "$HERE/.build/server.pid"
sleep 1
echo "==> 宿主 lite-server 已启动 ($PORT)"

# 5. 等容器跑完
wait $NSPAWN_PID || true
echo "==> 容器测试输出:"
cat "$HERE/.build/nspawn-test.log"
