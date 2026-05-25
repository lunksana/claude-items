#!/usr/bin/env bash
# PyReality 一键部署脚本
# 用法：bash install.sh
# 在项目目录内运行，自动完成环境检测、依赖安装、配置生成、系统服务注册。
set -euo pipefail

# ── 输出 ───────────────────────────────────────────────────────────────────────
_c() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info()  { echo "$(_c 36 "[*]") $*"; }
ok()    { echo "$(_c 32 "[✓]") $*"; }
warn()  { echo "$(_c 33 "[!]") $*"; }
err()   { echo "$(_c 31 "[✗]") $*"; exit 1; }
title() {
    local line; line=$(printf '═%.0s' {1..50})
    printf "\n\033[1;35m%s\n  %s\n%s\033[0m\n\n" "$line" "$*" "$line"
}

ask() {           # ask "提示" ["默认值"]  → 输出输入结果
    local prompt=$1 default=${2:-} hint=""
    [[ -n "$default" ]] && hint=" [$default]"
    read -rp "    ${prompt}${hint}: " val </dev/tty
    echo "${val:-$default}"
}

ask_yn() {        # ask_yn "提示" [y|n]  → 0=yes 1=no
    local prompt=$1 default=${2:-y}
    local hint; hint=$( [[ "$default" == y ]] && echo "Y/n" || echo "y/N" )
    read -rp "    ${prompt} (${hint}): " val </dev/tty
    val="${val:-$default}"
    [[ "$val" =~ ^[Yy] ]]
}

# ── 工作目录 ───────────────────────────────────────────────────────────────────
WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "${WORK_DIR}/server.py" && -f "${WORK_DIR}/client.py" ]] \
    || err "请在 PyReality 项目目录中运行此脚本"
cd "$WORK_DIR"

# ── 系统检测 ───────────────────────────────────────────────────────────────────
PKG_MGR=""

detect_os() {
    if   command -v apt-get &>/dev/null; then PKG_MGR="apt"
    elif command -v dnf     &>/dev/null; then PKG_MGR="dnf"
    elif command -v yum     &>/dev/null; then PKG_MGR="yum"
    elif command -v apk     &>/dev/null; then PKG_MGR="apk"
    else PKG_MGR="unknown"
    fi
    local os_name=""
    [[ -f /etc/os-release ]] && { . /etc/os-release; os_name="${PRETTY_NAME:-}"; }
    info "系统：${os_name:-unknown}  包管理器：${PKG_MGR}"
}

pkg_install() {
    case "$PKG_MGR" in
        apt) apt-get install -y -q "$@" ;;
        dnf) dnf install -y "$@" ;;
        yum) yum install -y "$@" ;;
        apk) apk add --no-cache "$@" ;;
        *)   warn "未知包管理器，请手动安装：$*" ;;
    esac
}

# ── Python ─────────────────────────────────────────────────────────────────────
PYTHON=""

check_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
                PYTHON="$cmd"
                ok "Python：$("$PYTHON" --version)"
                return 0
            fi
        fi
    done

    warn "未找到 Python 3.10+，尝试安装..."
    case "$PKG_MGR" in
        apt) pkg_install python3 python3-pip python3-venv ;;
        dnf) pkg_install python3 python3-pip ;;
        yum) pkg_install python3 python3-pip ;;
        apk) pkg_install python3 py3-pip ;;
        *)   err "请手动安装 Python 3.10+" ;;
    esac
    PYTHON="python3"
    ok "Python：$("$PYTHON" --version)"
}

install_pip_deps() {
    info "安装 Python 依赖（cryptography uvloop）..."
    "$PYTHON" -m pip install -q --upgrade pip 2>/dev/null || true
    "$PYTHON" -m pip install -q cryptography uvloop 2>/dev/null \
        || "$PYTHON" -m pip install -q cryptography
    ok "依赖安装完成"
}

# ── TCP Brutal ─────────────────────────────────────────────────────────────────
BRUTAL_OK=false

brutal_loaded() {
    "$PYTHON" -c "from core.brutal import is_available; exit(0 if is_available() else 1)" 2>/dev/null
}

suggest_rate_mbps() {
    local iface speed
    iface=$(ip route show default 2>/dev/null | grep -oP 'dev \K\S+' | head -1 || echo "eth0")
    speed=$(cat "/sys/class/net/${iface}/speed" 2>/dev/null || echo "0")
    if [[ "$speed" -gt 0 ]]; then
        local r=$(( speed / 10 ))
        echo $(( r < 5 ? 5 : ( r > 20 ? 20 : r ) ))
    else
        echo "8"
    fi
}

try_install_brutal() {
    if brutal_loaded; then
        ok "TCP Brutal 内核模块已就绪"
        BRUTAL_OK=true
        return 0
    fi
    warn "未检测到 tcp_brutal 内核模块"
    if ! ask_yn "是否安装 TCP Brutal？"; then
        info "跳过，将使用普通 TCP（brutal_rate_bps=0）"
        return 0
    fi
    [[ "$(id -u)" -eq 0 ]] || { warn "安装内核模块需要 root，已跳过"; return 0; }

    info "使用官方脚本安装 tcp_brutal..."
    if bash <(curl -fsSL https://tcp.hy2.sh/); then
        modprobe tcp_brutal 2>/dev/null || true
        if brutal_loaded; then
            ok "TCP Brutal 安装成功"
            BRUTAL_OK=true
        else
            warn "安装后模块仍未加载，将使用普通 TCP"
        fi
    else
        warn "安装脚本执行失败，将使用普通 TCP"
    fi
}

# ── 服务端配置 ─────────────────────────────────────────────────────────────────
configure_server() {
    title "服务端配置"

    local port password camouflage rate_bps=0

    port=$(ask "监听端口" "443")
    password=$(ask "连接密码（建议随机字符串）")
    [[ -n "$password" ]] || err "密码不能为空"
    camouflage=$(ask "伪装域名" "www.apple.com")

    try_install_brutal

    if $BRUTAL_OK; then
        local suggested; suggested=$(suggest_rate_mbps)
        info "检测建议速率：${suggested} Mbps"
        local rate_mbps; rate_mbps=$(ask "单连接 Brutal 速率 (Mbps)" "$suggested")
        rate_bps=$(( rate_mbps * 1_000_000 ))
    fi

    export _PORT="$port" _PASSWORD="$password" _CAMOUFLAGE="$camouflage" _RATE="$rate_bps"
    "$PYTHON" - << 'PYEOF'
import json, os, sys
cfg = {
    "listen_host":     "0.0.0.0",
    "listen_port":     int(os.environ["_PORT"]),
    "password":        os.environ["_PASSWORD"],
    "camouflage_host": os.environ["_CAMOUFLAGE"],
    "camouflage_port": 443,
    "brutal_rate_bps": int(os.environ["_RATE"]),
}
with open("config_server.json", "w") as f:
    json.dump(cfg, f, indent=4)
print("    config_server.json 已生成")
PYEOF
    ok "服务端配置完成"
}

# ── 客户端配置 ─────────────────────────────────────────────────────────────────
configure_client() {
    title "客户端配置"

    local server_host server_port password camouflage socks5_port
    local dns_port=0 cn_dns="223.5.5.5" remote_dns="8.8.8.8"

    server_host=$(ask "服务端 IP 或域名")
    [[ -n "$server_host" ]] || err "服务端地址不能为空"
    server_port=$(ask "服务端端口" "443")
    password=$(ask "连接密码（与服务端一致）")
    [[ -n "$password" ]] || err "密码不能为空"
    camouflage=$(ask "伪装域名（与服务端一致）" "www.apple.com")
    socks5_port=$(ask "本地 SOCKS5 端口" "1080")

    if ask_yn "是否启用本地 DNS 转发器（防止 DNS 泄漏）？"; then
        dns_port=$(ask "DNS 监听端口（5353 无需 root，53 需要 root）" "5353")
        cn_dns=$(ask "国内 DNS 服务器" "223.5.5.5")
        remote_dns=$(ask "境外 DNS 服务器（通过隧道）" "8.8.8.8")
        info "启动后将系统 DNS 改为 127.0.0.1:${dns_port}"
    fi

    export _SHOST="$server_host" _SPORT="$server_port" _PWD="$password" \
           _CAM="$camouflage"    _S5PORT="$socks5_port" \
           _DNSPORT="$dns_port"  _CNDNS="$cn_dns" _RDNS="$remote_dns"
    "$PYTHON" - << 'PYEOF'
import json, os
cfg = {
    "socks5_host":      "0.0.0.0",
    "socks5_port":      int(os.environ["_S5PORT"]),
    "server_host":      os.environ["_SHOST"],
    "server_port":      int(os.environ["_SPORT"]),
    "password":         os.environ["_PWD"],
    "camouflage_host":  os.environ["_CAM"],
    "brutal_rate_bps":  0,
    "brutal_pool_size": 10,
    "tproxy_port":      0,
    "dns_listen_host":  "127.0.0.1",
    "dns_listen_port":  int(os.environ["_DNSPORT"]),
    "cn_dns":           os.environ["_CNDNS"],
    "remote_dns":       os.environ["_RDNS"],
    "geosite_dir":         ".geosite",
    "geosite_update_days": 7,
    "geosite_sources": [{"name": "loyalsoldier", "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"}],
    "geoip_sources":   [{"name": "loyalsoldier", "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"}],
    "rules": [
        "GEOSITE,loyalsoldier:category-ads-all,REJECT",
        "GEOSITE,loyalsoldier:private,DIRECT",
        "GEOIP,loyalsoldier:private,DIRECT",
        "GEOSITE,loyalsoldier:cn,DIRECT",
        "GEOSITE,loyalsoldier:apple-cn,DIRECT",
        "GEOSITE,loyalsoldier:google-cn,DIRECT",
        "GEOIP,loyalsoldier:cn,DIRECT",
        "IP-CIDR,127.0.0.0/8,DIRECT",
        "IP-CIDR,10.0.0.0/8,DIRECT",
        "IP-CIDR,172.16.0.0/12,DIRECT",
        "IP-CIDR,192.168.0.0/16,DIRECT",
        "FINAL,PROXY",
    ],
}
with open("config_client.json", "w") as f:
    json.dump(cfg, f, indent=4)
print("    config_client.json 已生成")
PYEOF
    ok "客户端配置完成"
}

# ── 系统服务 ───────────────────────────────────────────────────────────────────
install_systemd() {
    local role=$1      # server | client
    local name="pyrealiy-${role}"
    local desc; [[ "$role" == server ]] && desc="PyReality 代理服务端" || desc="PyReality 代理客户端"
    local unit="/etc/systemd/system/${name}.service"

    cat > "$unit" << EOF
[Unit]
Description=${desc}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${WORK_DIR}
ExecStart=${PYTHON} ${WORK_DIR}/${role}.py ${WORK_DIR}/config_${role}.json
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$name"
    ok "systemd 服务已注册：${name}（开机自启已启用）"
    info "启动：systemctl start ${name}"
    info "状态：systemctl status ${name}"
    info "日志：journalctl -u ${name} -f"

    if ask_yn "是否立即启动服务？" "n"; then
        systemctl start "$name"
        ok "服务已启动"
    fi
}

try_install_service() {
    local role=$1
    if ! command -v systemctl &>/dev/null; then
        warn "未检测到 systemd，跳过服务安装"
        return 0
    fi
    [[ "$(id -u)" -eq 0 ]] || { warn "安装系统服务需要 root，已跳过"; return 0; }
    if ask_yn "是否安装为 systemd 系统服务（开机自启）？" "n"; then
        install_systemd "$role"
    fi
}

# ── 主流程 ─────────────────────────────────────────────────────────────────────
main() {
    title "PyReality 一键部署"
    detect_os
    check_python
    install_pip_deps

    local role
    role=$(ask "部署角色：[1] 服务端（墙外 VPS）  [2] 客户端（本地）" "1")

    if [[ "$role" == "2" ]]; then
        configure_client
        try_install_service "client"
        echo
        info "手动启动命令："
        echo "    $PYTHON $WORK_DIR/client.py"
    else
        configure_server
        try_install_service "server"
        echo
        info "手动启动命令："
        echo "    $PYTHON $WORK_DIR/server.py"
    fi

    echo
    ok "部署完成！如需高级配置（TProxy、分流规则、多源 GeoSite），运行 python setup.py"
}

main "$@"
