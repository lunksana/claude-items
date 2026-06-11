#!/usr/bin/env bash
# PyRealiy 一键安装向导
#
# 用法：
#   sudo bash install.sh
#
# 在项目目录内运行。交互式引导：
#   - 选择安装类型（服务端 / 客户端 / 两端）
#   - 服务端：监听端口、密码、伪装 SNI、Brutal 内核模块、systemd unit；
#            末尾自动打印对应客户端配置模板
#   - 客户端：服务端地址、密码、SNI、SOCKS5 端口、
#            **DNS 方案**（默认国内 119.29.29.29 直连，国外走 VPS 转发 1.1.1.1）、
#            **路由模板**（默认国内外分流）、Clash API、systemd unit
#
# 单一安装入口；旧 setup.py 已淘汰。
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# 输出 / 交互
# ──────────────────────────────────────────────────────────────────────────────

_c() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info()  { echo "$(_c 36 "[*]") $*"; }
ok()    { echo "$(_c 32 "[✓]") $*"; }
warn()  { echo "$(_c 33 "[!]") $*"; }
err()   { echo "$(_c 31 "[✗]") $*" >&2; exit 1; }
title() {
    local line; line=$(printf '═%.0s' {1..56})
    printf "\n\033[1;35m%s\n  %s\n%s\033[0m\n\n" "$line" "$*" "$line"
}

ask() {                              # ask "提示" ["默认"]  → 输出
    local prompt=$1 default=${2:-} hint=""
    [[ -n "$default" ]] && hint=" [$default]"
    local val
    read -rp "    ${prompt}${hint}: " val </dev/tty
    echo "${val:-$default}"
}

ask_yn() {                           # ask_yn "提示" [y|n]  → 0/1
    local prompt=$1 default=${2:-y} hint val
    hint=$( [[ "$default" == y ]] && echo "Y/n" || echo "y/N" )
    read -rp "    ${prompt} (${hint}): " val </dev/tty
    val="${val:-$default}"
    [[ "$val" =~ ^[Yy] ]]
}

ask_choice() {                       # ask_choice "提示" "选项1" "选项2" ... → 1-based 数字
    local prompt=$1 ; shift
    local options=("$@") i n=${#options[@]} val
    echo "    $prompt" >&2
    for ((i = 0; i < n; i++)); do
        printf "      %d) %s\n" $((i + 1)) "${options[$i]}" >&2
    done
    while :; do
        read -rp "    选择 [1-$n] (默认 1): " val </dev/tty
        val="${val:-1}"
        if [[ "$val" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= n )); then
            echo "$val"
            return
        fi
        echo "    无效输入。" >&2
    done
}

ask_port() {                         # ask_port "提示" "默认"
    local prompt=$1 default=$2 val
    while :; do
        val=$(ask "$prompt" "$default")
        if [[ "$val" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= 65535 )); then
            echo "$val"
            return
        fi
        warn "端口须为 1-65535 整数。"
    done
}

# ──────────────────────────────────────────────────────────────────────────────
# 系统检测 / 基础依赖
# ──────────────────────────────────────────────────────────────────────────────

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "${WORK_DIR}/server.py" && -f "${WORK_DIR}/client.py" ]] \
    || err "请在 PyRealiy 项目目录中运行此脚本（看不到 server.py / client.py）"
cd "$WORK_DIR"

PYREALIY_VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' core/version.py 2>/dev/null || echo "unknown")"
PYREALIY_USER="${SUDO_USER:-$USER}"

PKG_MGR=""
detect_pkg_mgr() {
    if   command -v apt-get &>/dev/null; then PKG_MGR="apt"
    elif command -v dnf     &>/dev/null; then PKG_MGR="dnf"
    elif command -v yum     &>/dev/null; then PKG_MGR="yum"
    elif command -v apk     &>/dev/null; then PKG_MGR="apk"
    else PKG_MGR="unknown"; fi
}

check_root()  { [[ $EUID -eq 0 ]] || err "需要 root 权限：sudo bash install.sh"; }
check_linux() { [[ "$(uname -s)" == "Linux" ]] || err "仅支持 Linux"; }

check_python() {
    command -v python3 &>/dev/null || err "未找到 python3。请先安装 Python 3.10+"
    local v
    v=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    info "Python 版本：$v"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || err "需要 Python 3.10+（当前 $v）"
}

check_openssl() {
    command -v openssl &>/dev/null || err "未找到 openssl"
}

install_pip_deps() {
    info "安装 Python 依赖（cryptography, uvloop）..."
    if ! python3 -m pip --version &>/dev/null; then
        case $PKG_MGR in
            apt) apt-get install -y python3-pip ;;
            dnf|yum) "$PKG_MGR" install -y python3-pip ;;
            apk) apk add --no-cache py3-pip ;;
            *) err "需要 pip。请手装 python3-pip 后重试。" ;;
        esac
    fi
    # PEP 668：Debian 13/Ubuntu 24+/最近的 Fedora 默认禁止全局 pip，要 --break-system-packages。
    # 老系统 pip < 23.0.1 不识别该 flag —— 仅在检测到 EXTERNALLY-MANAGED 标志时才加。
    local pip_args=""
    if compgen -G "/usr/lib/python3*/EXTERNALLY-MANAGED" > /dev/null 2>&1 \
       || compgen -G "/usr/lib/python3.*/EXTERNALLY-MANAGED" > /dev/null 2>&1; then
        pip_args="--break-system-packages"
        info "  检测到 PEP 668 environment，加 --break-system-packages"
    fi
    python3 -m pip install $pip_args -r requirements.txt --quiet
    # uvloop 可选但强推（性能 +18%）
    python3 -m pip install $pip_args uvloop --quiet || warn "uvloop 安装失败，继续（性能受影响）"
    ok "Python 依赖装好"
}

# ──────────────────────────────────────────────────────────────────────────────
# 伪装 SNI 探测（openssl TLS 1.3 真握手）
# ──────────────────────────────────────────────────────────────────────────────

probe_camouflage() {
    local host=$1 port=${2:-443}
    info "探测 $host:$port TLS 1.3 支持..."
    local out
    out=$(timeout 10 openssl s_client -connect "${host}:${port}" -servername "$host" \
          -tls1_3 -no_ign_eof < /dev/null 2>&1 || true)
    if echo "$out" | grep -q "Protocol  : TLSv1.3"; then
        ok "$host 支持 TLS 1.3，握手成功"
        return 0
    fi
    warn "$host 看起来不支持 TLS 1.3（或网络不通）—— 服务端可能出现 'Handshake cache warmup failed'"
    return 1
}

ask_camouflage_host() {
    local default=${1:-www.apple.com}
    local val
    while :; do
        val=$(ask "伪装 SNI（pyrealiy 会从该域名拉真实 TLS 握手做反指纹）" "$default")
        if probe_camouflage "$val"; then
            echo "$val"
            return
        fi
        ask_yn "继续使用 $val 吗？（不推荐）" n && { echo "$val"; return; }
    done
}

# ──────────────────────────────────────────────────────────────────────────────
# Brutal（拥塞控制，可选）
# ──────────────────────────────────────────────────────────────────────────────

brutal_loaded() {
    [[ -f /proc/sys/net/ipv4/tcp_available_congestion_control ]] && \
        grep -qw brutal /proc/sys/net/ipv4/tcp_available_congestion_control
}

handle_brutal_optional() {
    info "Brutal 是给单条连接定速的内核模块（Hysteria2 思路），可选"
    if brutal_loaded; then
        ok "已检测到 Brutal 内核模块"
        return 0
    fi
    if ask_yn "未检测到 Brutal。需要为本机安装吗？（自建 VPS 推荐）" n; then
        warn "Brutal 安装较复杂（需匹配内核版本），跳过细节"
        warn "参考：https://github.com/apernet/tcp-brutal"
        ask_yn "现在打开浏览器查文档（手动按文档装）后重试，还是继续不装？" n || true
        return 1
    fi
    return 1
}

# ──────────────────────────────────────────────────────────────────────────────
# 写配置文件
# ──────────────────────────────────────────────────────────────────────────────

write_server_config() {
    local listen_host=$1 listen_port=$2 password=$3 camouflage=$4 brutal_rate_bps=$5
    # 注意：服务端目前仍用老平铺 schema（不写 schema_version 字段）。
    # 写 schema_version=1 + 老顶层字段会触发 load_config 的"unknown top-level keys" WARN。
    # 服务端的 schema_v1 化是未来工作，schema_v0 → v1 自动识别仍由 _LEGACY_TOP_KEYS 覆盖。
    cat > "$WORK_DIR/config_server.json" <<EOF
{
    "listen_host": "${listen_host}",
    "listen_port": ${listen_port},
    "password": "${password}",
    "camouflage_host": "${camouflage}",
    "brutal_rate_bps": ${brutal_rate_bps},
    "log": {"format": "text"}
}
EOF
    ok "已生成 config_server.json"
}

write_client_config() {
    local server_host=$1 server_port=$2 password=$3 camouflage=$4
    local socks5_port=$5 routing_preset=$6 dns_preset=$7 enable_api=$8 api_secret=$9
    local routing_json dns_extra api_extra

    case $routing_preset in
        transparent) routing_json='{"default": "proxy", "rules": []}' ;;
        china_split)
            routing_json='{
            "default": "proxy",
            "rules": [
                {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"], "outbound": "direct"},
                {"geosite": ["loyalsoldier:cn"], "outbound": "direct"},
                {"geoip":   ["loyalsoldier:cn"], "outbound": "direct"}
            ]
        }'
            ;;
        custom) routing_json='{"default": "proxy", "rules": []}' ;;
    esac

    # DNS section（写在顶层老格式字段，与现有 dns_forwarder.py 兼容）
    case $dns_preset in
        off)
            dns_extra=""
            ;;
        china_split)
            dns_extra=',
    "dns_listen_host": "127.0.0.1",
    "dns_listen_port": 5353,
    "cn_dns": "119.29.29.29",
    "remote_dns": "1.1.1.1:53"'
            ;;
        full_proxy)
            dns_extra=',
    "dns_listen_host": "127.0.0.1",
    "dns_listen_port": 5353,
    "cn_dns": "1.1.1.1:53",
    "remote_dns": "1.1.1.1:53"'
            ;;
    esac

    if [[ "$enable_api" == "yes" ]]; then
        api_extra=',
    "api": {
        "listen": "127.0.0.1:9090",
        "secret": "'"$api_secret"'",
        "cors": ["*"]
    }'
    else
        api_extra=""
    fi

    cat > "$WORK_DIR/config_client.json" <<EOF
{
    "schema_version": 1,
    "inbounds": [
        {"type": "socks5", "listen": "127.0.0.1:${socks5_port}"}
    ],
    "outbounds": [
        {
            "tag": "proxy",
            "type": "pyrealiy",
            "server_host": "${server_host}",
            "server_port": ${server_port},
            "password": "${password}",
            "camouflage_host": "${camouflage}"
        },
        {"tag": "direct", "type": "direct"},
        {"tag": "block",  "type": "block"}
    ],
    "route": ${routing_json},
    "log": {"format": "text"}${dns_extra}${api_extra}
}
EOF
    ok "已生成 config_client.json"
    if [[ "$routing_preset" == "custom" ]]; then
        warn "你选择了自定义路由：请编辑 config_client.json 的 route.rules 数组"
        warn "字段格式见 README 的「路由 schema」一节"
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Init 系统检测 + service unit 安装（systemd / OpenRC / SysV init.d）
# ──────────────────────────────────────────────────────────────────────────────

# 全局变量：由 detect_init_system 设置
INIT_SYSTEM=""        # systemd | openrc | sysv | unknown

detect_init_system() {
    if [[ -d /run/systemd/system ]] || command -v systemctl &>/dev/null; then
        INIT_SYSTEM="systemd"
    elif command -v rc-service &>/dev/null && command -v rc-update &>/dev/null; then
        INIT_SYSTEM="openrc"
    elif [[ -d /etc/init.d ]] && (command -v update-rc.d &>/dev/null || command -v chkconfig &>/dev/null); then
        INIT_SYSTEM="sysv"
    else
        INIT_SYSTEM="unknown"
    fi
    info "检测到 init 系统：${INIT_SYSTEM}"
}

install_service_unit() {
    local kind=$1   # server 或 client
    case "$INIT_SYSTEM" in
        systemd) _install_systemd "$kind" ;;
        openrc)  _install_openrc  "$kind" ;;
        sysv)    _install_sysv    "$kind" ;;
        unknown) _install_skip    "$kind" ;;
    esac
}

# ── systemd ──
_install_systemd() {
    local kind=$1
    local unit="/etc/systemd/system/pyrealiy-${kind}.service"
    info "写 systemd unit：$unit"
    cat > "$unit" <<EOF
[Unit]
Description=PyRealiy ${kind^}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${WORK_DIR}
ExecStart=/usr/bin/python3 ${WORK_DIR}/${kind}.py ${WORK_DIR}/config_${kind}.json
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=3
StandardOutput=append:${WORK_DIR}/${kind}.log
StandardError=append:${WORK_DIR}/${kind}.log
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "pyrealiy-${kind}.service" >/dev/null
    ok "pyrealiy-${kind}.service 已 enable（开机自启）"
    if ask_yn "现在启动 pyrealiy-${kind}.service 吗？" y; then
        systemctl restart "pyrealiy-${kind}.service"
        sleep 2
        if systemctl is-active --quiet "pyrealiy-${kind}.service"; then
            ok "pyrealiy-${kind}.service 已运行"
            info "查看状态：systemctl status pyrealiy-${kind}"
            info "查看日志：tail -f ${WORK_DIR}/${kind}.log"
            info "重载配置：systemctl reload pyrealiy-${kind}"
        else
            warn "服务启动失败。查日志：tail -50 ${WORK_DIR}/${kind}.log"
        fi
    fi
}

# ── OpenRC（Alpine / Gentoo）──
_install_openrc() {
    local kind=$1
    local unit="/etc/init.d/pyrealiy-${kind}"
    info "写 OpenRC service：$unit"
    cat > "$unit" <<EOF
#!/sbin/openrc-run
# PyRealiy ${kind^} OpenRC service

name="PyRealiy ${kind^}"
description="PyRealiy ${kind^} service"

command="/usr/bin/python3"
command_args="${WORK_DIR}/${kind}.py ${WORK_DIR}/config_${kind}.json"
command_background=true
pidfile="/run/pyrealiy-${kind}.pid"
output_log="${WORK_DIR}/${kind}.log"
error_log="${WORK_DIR}/${kind}.log"
directory="${WORK_DIR}"

# 提高 fd limit（与 systemd LimitNOFILE=65536 对齐）
rc_ulimit="-n 65536"

depend() {
    need net
    after net
}

reload() {
    ebegin "Reloading \${RC_SVCNAME} (SIGHUP for hot-reload)"
    start-stop-daemon --signal HUP --pidfile "\${pidfile}"
    eend \$?
}
EOF
    chmod +x "$unit"
    rc-update add "pyrealiy-${kind}" default >/dev/null
    ok "pyrealiy-${kind} 已加入 default runlevel（开机自启）"
    if ask_yn "现在启动 pyrealiy-${kind} 吗？" y; then
        rc-service "pyrealiy-${kind}" restart >/dev/null 2>&1 || \
        rc-service "pyrealiy-${kind}" start
        sleep 2
        if rc-service "pyrealiy-${kind}" status &>/dev/null; then
            ok "pyrealiy-${kind} 已运行"
            info "查看状态：rc-service pyrealiy-${kind} status"
            info "查看日志：tail -f ${WORK_DIR}/${kind}.log"
            info "重载配置：rc-service pyrealiy-${kind} reload"
        else
            warn "服务启动失败。查日志：tail -50 ${WORK_DIR}/${kind}.log"
        fi
    fi
}

# ── SysV init.d ──
_install_sysv() {
    local kind=$1
    local unit="/etc/init.d/pyrealiy-${kind}"
    info "写 SysV init script：$unit"
    cat > "$unit" <<EOF
#!/bin/bash
### BEGIN INIT INFO
# Provides:          pyrealiy-${kind}
# Required-Start:    \$network \$remote_fs
# Required-Stop:     \$network \$remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: PyRealiy ${kind^}
# Description:       PyRealiy ${kind^} (TLS-camouflage proxy)
### END INIT INFO

NAME=pyrealiy-${kind}
DAEMON=/usr/bin/python3
DAEMON_ARGS="${WORK_DIR}/${kind}.py ${WORK_DIR}/config_${kind}.json"
WORKDIR="${WORK_DIR}"
PIDFILE=/var/run/\$NAME.pid
LOGFILE=${WORK_DIR}/${kind}.log

ulimit -n 65536 2>/dev/null || true

is_running() {
    [[ -f \$PIDFILE ]] && kill -0 "\$(cat \$PIDFILE)" 2>/dev/null
}

case "\$1" in
    start)
        if is_running; then
            echo "\$NAME already running (pid \$(cat \$PIDFILE))"
            exit 0
        fi
        echo -n "Starting \$NAME... "
        cd "\$WORKDIR"
        nohup \$DAEMON \$DAEMON_ARGS >> "\$LOGFILE" 2>&1 &
        echo \$! > "\$PIDFILE"
        sleep 1
        if is_running; then
            echo "ok (pid \$(cat \$PIDFILE))"
        else
            echo "failed (see \$LOGFILE)"
            rm -f "\$PIDFILE"
            exit 1
        fi
        ;;
    stop)
        if ! is_running; then
            echo "\$NAME not running"
            exit 0
        fi
        echo -n "Stopping \$NAME... "
        kill "\$(cat \$PIDFILE)"
        for i in 1 2 3 4 5; do
            is_running || break
            sleep 1
        done
        is_running && kill -9 "\$(cat \$PIDFILE)" 2>/dev/null
        rm -f "\$PIDFILE"
        echo "ok"
        ;;
    restart)
        \$0 stop
        \$0 start
        ;;
    reload)
        is_running && kill -HUP "\$(cat \$PIDFILE)" && echo "\$NAME reload signaled" || \\
            { echo "\$NAME not running"; exit 1; }
        ;;
    status)
        if is_running; then
            echo "\$NAME running (pid \$(cat \$PIDFILE))"
            exit 0
        else
            echo "\$NAME stopped"
            exit 1
        fi
        ;;
    *)
        echo "Usage: \$0 {start|stop|restart|reload|status}"
        exit 1
        ;;
esac
EOF
    chmod +x "$unit"
    if command -v update-rc.d &>/dev/null; then
        update-rc.d "pyrealiy-${kind}" defaults >/dev/null
    elif command -v chkconfig &>/dev/null; then
        chkconfig --add "pyrealiy-${kind}" 2>/dev/null || true
        chkconfig "pyrealiy-${kind}" on
    else
        warn "未找到 update-rc.d / chkconfig；不自动 enable，手动加 runlevel symlink"
    fi
    ok "pyrealiy-${kind} 已 enable（开机自启）"
    if ask_yn "现在启动 pyrealiy-${kind} 吗？" y; then
        service "pyrealiy-${kind}" start
        sleep 2
        if service "pyrealiy-${kind}" status >/dev/null; then
            ok "pyrealiy-${kind} 已运行"
            info "查看状态：service pyrealiy-${kind} status"
            info "查看日志：tail -f ${WORK_DIR}/${kind}.log"
            info "重载配置：service pyrealiy-${kind} reload"
        else
            warn "服务启动失败。查日志：tail -50 ${WORK_DIR}/${kind}.log"
        fi
    fi
}

# ── 兜底：未知 init ──
_install_skip() {
    local kind=$1
    warn "未识别 init 系统（既无 systemd / openrc / sysv 工具）；跳过 service 安装。"
    warn "手动启动：cd ${WORK_DIR} && python3 ${kind}.py config_${kind}.json"
    warn "热加载配置：kill -HUP \$(pgrep -f \"python3.*${kind}.py\")"
}

# ──────────────────────────────────────────────────────────────────────────────
# 服务端流程
# ──────────────────────────────────────────────────────────────────────────────

install_server() {
    title "服务端安装"

    local listen_host listen_port password camouflage brutal_rate_bps=0
    listen_host=$(ask "监听地址（0.0.0.0 = 所有网卡）" "0.0.0.0")
    listen_port=$(ask_port "监听端口" "443")

    if ask_yn "自动生成密码？" y; then
        password=$(openssl rand -base64 24 | tr -d /+= | head -c 24)
        info "已生成密码：$password"
    else
        password=$(ask "密码")
        [[ -z "$password" ]] && err "密码不能为空"
    fi

    camouflage=$(ask_camouflage_host "www.apple.com")

    if handle_brutal_optional; then
        local default_rate=$((10 * 1000 * 1000))
        local rate_mbps
        rate_mbps=$(ask "每条连接的 Brutal 速率上限（Mbps）" "10")
        brutal_rate_bps=$(( rate_mbps * 1000 * 1000 ))
        info "Brutal 速率：${rate_mbps} Mbps / connection"
    fi

    write_server_config "$listen_host" "$listen_port" "$password" "$camouflage" "$brutal_rate_bps"
    install_service_unit "server"

    # 末尾：打印对应客户端 cfg 模板，让用户直接复制到客户端
    title "客户端配置模板（复制到客户端机器的 config_client.json）"
    local server_ip
    server_ip=$(curl -s -4 --max-time 5 https://api.ipify.org 2>/dev/null || echo "<你的服务端公网 IP>")
    cat <<EOF
$(_c 36 "请用下面的配置在客户端 install.sh 引导时输入对应字段：")

    服务端地址  : $(_c 33 "$server_ip")
    服务端端口  : $(_c 33 "$listen_port")
    密码        : $(_c 33 "$password")
    伪装 SNI    : $(_c 33 "$camouflage")

$(_c 36 "或者直接把下面的 config_client.json 模板拷到客户端（最简版，无 DNS / 路由分流）：")

{
    "schema_version": 1,
    "inbounds": [{"type": "socks5", "listen": "127.0.0.1:1080"}],
    "outbounds": [
        {
            "tag": "proxy", "type": "pyrealiy",
            "server_host": "$server_ip",
            "server_port": $listen_port,
            "password": "$password",
            "camouflage_host": "$camouflage"
        },
        {"tag": "direct", "type": "direct"},
        {"tag": "block",  "type": "block"}
    ],
    "route": {"default": "proxy", "rules": []}
}

$(_c 36 "传到客户端建议用 scp（更安全），不要明文走 IM：")
    $(_c 33 "scp config_client.json user@<客户端机器>:~/")

EOF
}

# ──────────────────────────────────────────────────────────────────────────────
# 客户端流程
# ──────────────────────────────────────────────────────────────────────────────

install_client() {
    title "客户端安装"

    local server_host server_port password camouflage socks5_port
    server_host=$(ask "服务端地址（IP 或域名）" "")
    [[ -z "$server_host" ]] && err "服务端地址不能为空"
    server_port=$(ask_port "服务端端口" "443")
    password=$(ask "密码（与服务端一致）")
    [[ -z "$password" ]] && err "密码不能为空"
    camouflage=$(ask "伪装 SNI（与服务端一致）" "www.apple.com")
    socks5_port=$(ask_port "本地 SOCKS5 监听端口" "1080")

    # ── 路由模板 ──
    echo
    info "路由模板：决定哪些流量走代理"
    local choice routing_preset
    choice=$(ask_choice "选择" \
        "国内外分流（推荐）：geosite:cn / 内网 → 直连，其余走代理" \
        "全代理：所有流量走 proxy 出口" \
        "自定义：生成空 rules 数组，安装后自己编辑")
    case $choice in
        1) routing_preset="china_split" ;;
        2) routing_preset="transparent" ;;
        3) routing_preset="custom" ;;
    esac

    # ── DNS 方案 ──
    echo
    info "DNS 方案：决定 DNS 查询怎么走"
    info "  默认推荐：国内域名查 119.29.29.29 直连（快）；其余走 VPS 转发到 1.1.1.1（防污染）"
    local dns_preset
    if [[ "$routing_preset" == "transparent" ]]; then
        # 全代理路由下 DNS 也无脑全代理（geosite 规则用不上）
        choice=$(ask_choice "选择" \
            "全代理：所有 DNS 走 proxy 隧道到 1.1.1.1（推荐）" \
            "不启用 DNS forwarder（系统继续用 /etc/resolv.conf）")
        case $choice in 1) dns_preset="full_proxy" ;; 2) dns_preset="off" ;; esac
    else
        choice=$(ask_choice "选择" \
            "国内外分流（推荐）：cn 直查 119.29.29.29，其余走 VPS 转发 1.1.1.1" \
            "全代理：所有 DNS 走 proxy 隧道到 1.1.1.1（隐私优先，速度较慢）" \
            "不启用 DNS forwarder")
        case $choice in
            1) dns_preset="china_split" ;;
            2) dns_preset="full_proxy" ;;
            3) dns_preset="off" ;;
        esac
    fi

    # ── Clash API ──
    echo
    info "Clash 兼容 API（可让 Yacd / metacubexd 之类 UI 接管面板）"
    local enable_api=no api_secret=""
    if ask_yn "启用 Clash API？" y; then
        enable_api=yes
        api_secret=$(openssl rand -base64 24 | tr -d /+= | head -c 24)
        info "API secret 已生成：$api_secret"
        info "Yacd 登录信息：host=127.0.0.1  port=9090  secret=$api_secret"
    fi

    write_client_config "$server_host" "$server_port" "$password" "$camouflage" \
                        "$socks5_port" "$routing_preset" "$dns_preset" \
                        "$enable_api" "$api_secret"

    install_service_unit "client"

    # 末尾提示
    title "客户端安装完成"
    cat <<EOF
$(_c 32 "✓ SOCKS5") 已监听 127.0.0.1:${socks5_port}
EOF
    if [[ "$dns_preset" != "off" ]]; then
        echo "$(_c 32 "✓ DNS")    forwarder 监听 127.0.0.1:5353"
        echo "    将系统 DNS 改成 127.0.0.1（或用 iptables 重定向 53→5353）"
    fi
    if [[ "$enable_api" == "yes" ]]; then
        echo "$(_c 32 "✓ Clash API") 监听 127.0.0.1:9090"
        echo "    secret: $api_secret"
    fi
    cat <<EOF

应用层用法：
  - Chrome 等：设置 SOCKS5 代理 127.0.0.1:${socks5_port}
  - curl: curl -x socks5h://127.0.0.1:${socks5_port} https://www.google.com
  - 系统级（透明）：需要额外的 iptables/redsocks 或 TUN（pyrealiy 当前未内置）

常用命令（基于检测到的 init 系统 ${INIT_SYSTEM}）：
$(case "$INIT_SYSTEM" in
    systemd) echo "  systemctl status pyrealiy-client          # 状态"
             echo "  systemctl reload pyrealiy-client          # 热加载配置" ;;
    openrc)  echo "  rc-service pyrealiy-client status         # 状态"
             echo "  rc-service pyrealiy-client reload         # 热加载配置" ;;
    sysv)    echo "  service pyrealiy-client status            # 状态"
             echo "  service pyrealiy-client reload            # 热加载配置" ;;
    *)       echo "  kill -HUP \$(pgrep -f 'python3.*client.py')  # 热加载配置" ;;
esac)
  tail -f client.log                       # 日志
EOF
}

# ──────────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────────

main() {
    title "PyRealiy v${PYREALIY_VERSION} 安装向导"

    check_linux
    check_root
    detect_pkg_mgr
    detect_init_system
    check_python
    check_openssl

    local choice mode
    choice=$(ask_choice "安装类型" "服务端" "客户端" "两端都装（本地测试用）")
    case $choice in
        1) mode="server" ;;
        2) mode="client" ;;
        3) mode="both" ;;
    esac

    install_pip_deps

    case $mode in
        server) install_server ;;
        client) install_client ;;
        both)
            install_server
            echo
            warn "服务端装完，现在装客户端（用你刚才看到的配置）"
            ask_yn "继续吗？" y && install_client
            ;;
    esac

    title "全部完成"
    ok "感谢使用 PyRealiy v${PYREALIY_VERSION}"
    cat <<EOF

    高级配置请参考带注释的示例文件：
      config_server.example.jsonc
      config_client.example.jsonc

    schema 合约 / Clash API / 热加载 / DoH-DoT 详见 README.md
EOF
}

main "$@"
