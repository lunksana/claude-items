#!/usr/bin/env bash
# Mirage 一键安装向导
#
# 用法：
#   sudo bash install.sh
#
# 在项目目录内运行。交互式引导：
#   - 选择安装类型（服务端 / 客户端 / 两端）
#   - 服务端：监听端口、密码、伪装 SNI、Brutal 内核模块、systemd unit；
#            末尾自动打印对应客户端配置模板
#   - 客户端：服务端地址、密码、SNI、SOCKS5/mixed 端口、监听地址（LAN 共用）、
#            **DNS 方案**（默认国内 119.29.29.29 直连，国外走 VPS 转发 1.1.1.1）、
#            **路由模板**（国内外分流 / 全代理 / 逐项 geo tag 高级 / 空规则）、
#            可选 TProxy、Clash API、systemd/OpenRC/SysV unit
#
# 本向导生成**单节点**客户端配置（一个 mirage 出口）。多节点 + urltest /
# fallback / selector 选路组、多字段 AND 规则等高级用法需手动编辑配置，
# 详见 README.md「多节点与自适应选路」「分流规则」两节与 config_client.example.jsonc。
#
# 单一安装入口；旧 setup.py 已淘汰。
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# 输出 / 交互
# ──────────────────────────────────────────────────────────────────────────────

_c() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
# 所有交互式提示 / 进度消息走 stderr，避免被 $(funcname) 捕获污染数据
info()  { echo "$(_c 36 "[*]") $*" >&2; }
ok()    { echo "$(_c 32 "[✓]") $*" >&2; }
warn()  { echo "$(_c 33 "[!]") $*" >&2; }
err()   { echo "$(_c 31 "[✗]") $*" >&2; exit 1; }
title() {
    local line; line=$(printf '═%.0s' {1..56})
    printf "\n\033[1;35m%s\n  %s\n%s\033[0m\n\n" "$line" "$*" "$line" >&2
}

ask() {                              # ask "提示" ["默认"]  → 输出
    local prompt=$1 default=${2:-} hint=""
    [[ -n "$default" ]] && hint=" [$default]"
    local val
    read -rp "    ${prompt}${hint}: " val </dev/tty
    echo "${val:-$default}"
}

ask_secret() {                       # ask_secret "提示"  → 输出（输入不回显）
    local prompt=$1 val
    read -rsp "    ${prompt}: " val </dev/tty
    echo >&2                          # read -s 不换行，手动补一个
    echo "$val"
}

# 让用户手动设密码：隐藏输入 + 重输确认（防隐藏态打错却不自知）
ask_password_confirmed() {
    local p1 p2
    while :; do
        p1=$(ask_secret "密码")
        [[ -z "$p1" ]] && { warn "密码不能为空"; continue; }
        p2=$(ask_secret "再输一次确认")
        [[ "$p1" == "$p2" ]] && { echo "$p1"; return; }
        warn "两次输入不一致，请重来"
    done
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

ask_port() {                         # ask_port "提示" "默认" [tcp|udp|both]
    local prompt=$1 default=$2 proto=${3:-tcp} val
    while :; do
        val=$(ask "$prompt" "$default")
        if ! [[ "$val" =~ ^[0-9]+$ ]] || (( val < 1 || val > 65535 )); then
            warn "端口须为 1-65535 整数。"
            continue
        fi
        # 占用检测
        local in_use=""
        case "$proto" in
            tcp)  is_port_used "$val" tcp && in_use="TCP" ;;
            udp)  is_port_used "$val" udp && in_use="UDP" ;;
            both)
                is_port_used "$val" tcp && in_use="TCP"
                is_port_used "$val" udp && in_use="${in_use:+$in_use+}UDP"
                ;;
        esac
        if [[ -n "$in_use" ]]; then
            warn "端口 $val 已被占用（${in_use}）：$(ss -ltunp 2>/dev/null | awk -v p=":$val\$" '$5 ~ p {print $7; exit}')"
            if ask_yn "仍要用这个端口（启动可能失败）？" n; then
                echo "$val"
                return
            fi
            continue
        fi
        echo "$val"
        return
    done
}

# 检测端口是否被占用。$1=port, $2=tcp|udp
is_port_used() {
    local port=$1 proto=$2 flag
    case "$proto" in
        tcp) flag="-ltn" ;;
        udp) flag="-lun" ;;
        *)   return 1 ;;
    esac
    # ss 第 4 列是 Local Address:Port；匹配 [:.]port 结尾
    ss $flag 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
}

# ──────────────────────────────────────────────────────────────────────────────
# 系统检测 / 基础依赖
# ──────────────────────────────────────────────────────────────────────────────

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 卸载时不要求项目源在 cwd（卸载从已部署的 /opt/mirage/install.sh 运行也算）；
# 安装时检查在 source-action 里做
if ! [[ -f "${WORK_DIR}/server.py" && -f "${WORK_DIR}/client.py" ]]; then
    if [[ -d "/opt/mirage" && -f "/opt/mirage/server.py" ]]; then
        WORK_DIR="/opt/mirage"
    fi
fi
cd "$WORK_DIR" 2>/dev/null || cd /

MIRAGE_VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' "${WORK_DIR}/core/version.py" 2>/dev/null || echo "unknown")"
MIRAGE_USER="${SUDO_USER:-$USER}"

# ── FHS 路径常量 ──
INSTALL_PREFIX="/opt/mirage"
ETC_DIR="/etc/mirage"
LOG_DIR="/var/log/mirage"
STATE_DIR="/var/lib/mirage"
BIN_DIR="/usr/local/bin"

# Legacy paths（0.4.35 重命名前的 pyrealiy 项目）；uninstall 会一并清理
LEGACY_INSTALL_PREFIX="/opt/pyrealiy"
LEGACY_ETC_DIR="/etc/pyrealiy"
LEGACY_LOG_DIR="/var/log/pyrealiy"
LEGACY_STATE_DIR="/var/lib/pyrealiy"

# 由 choose_install_mode 设置：system | inplace
INSTALL_MODE=""
# 由 resolve_paths 设置：根据 INSTALL_MODE 计算实际部署路径
EFFECTIVE_LIB=""           # server.py / client.py 所在目录
EFFECTIVE_ETC=""           # cfg 文件所在目录
EFFECTIVE_LOG_DIR=""       # 日志目录
EFFECTIVE_GEOSITE_DIR=""   # geo 缓存目录（写进 client cfg）

# 由 ask_log_config 设置（cfg.log.format + logrotate 选项）
LOG_FORMAT="text"
LOG_LEVEL="INFO"          # 写入 cfg.log_levels.default
LOGROTATE_ENABLED="no"
LOGROTATE_MAXSIZE="100M"
LOGROTATE_KEEP="7"
LOGROTATE_COMPRESS="yes"

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
    command -v python3 &>/dev/null || err "未找到 python3。请先安装 Python 3.9+"
    local v
    v=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    info "Python 版本：$v"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || err "需要 Python 3.9+（当前 $v）"
}

check_openssl() {
    command -v openssl &>/dev/null || err "未找到 openssl"
}

install_pip_deps() {
    info "检查 Python 依赖（cryptography, uvloop）..."
    if python3 -c "import cryptography, uvloop" &>/dev/null; then
        ok "检测到 cryptography 与 uvloop 已安装且可用，跳过 pip 安装步骤。"
        return 0
    elif python3 -c "import cryptography" &>/dev/null; then
        ok "检测到 cryptography 已安装，尝试单独安装可选加速依赖 uvloop..."
        python3 -m pip install uvloop --prefer-binary --quiet 2>/dev/null || warn "uvloop 安装失败，继续（不影响核心功能）"
        return 0
    fi

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
    local pip_args="--prefer-binary"
    if compgen -G "/usr/lib/python3*/EXTERNALLY-MANAGED" > /dev/null 2>&1 \
       || compgen -G "/usr/lib/python3.*/EXTERNALLY-MANAGED" > /dev/null 2>&1; then
        pip_args="--break-system-packages --prefer-binary"
        info "  检测到 PEP 668 environment，加 --break-system-packages"
    fi
    python3 -m pip install $pip_args -r requirements.txt --quiet || err "安装 cryptography 失败！对于 32 位/老旧系统，请先通过系统包管理器 (如 apt install python3-cryptography) 手动安装。"
    # uvloop 可选但强推（性能 +18%）
    python3 -m pip install $pip_args uvloop --quiet || warn "uvloop 安装失败，继续（性能受影响）"
    ok "Python 依赖装好"
}

# ──────────────────────────────────────────────────────────────────────────────
# 节点 URI 编解码与解析 (mirage://)
# ──────────────────────────────────────────────────────────────────────────────

url_encode() {
    local LC_ALL=C
    local s=$1 out="" i ch
    for (( i=0; i<${#s}; i++ )); do
        ch="${s:i:1}"
        case "$ch" in
            [a-zA-Z0-9.~_-]) out+="$ch" ;;
            *) out+=$(printf '%%%02X' "'$ch") ;;
        esac
    done
    echo "$out"
}

url_decode() {
    local LC_ALL=C
    local s=$1
    s="${s//+/ }"
    printf '%b' "${s//%/\\x}"
}

build_node_uri() {
    local pwd=$1 host=$2 port=$3 sni=$4 brutal=${5:-0}
    local epwd esni q
    epwd=$(url_encode "$pwd")
    esni=$(url_encode "$sni")
    q="sni=${esni}"
    if [[ "$brutal" =~ ^[0-9]+$ ]] && (( brutal > 0 )); then
        q+="&brutal=$brutal"
    fi
    echo "mirage://${epwd}@${host}:${port}?${q}"
}

parse_node_uri() {
    local uri=$1
    NODE_PWD=""; NODE_HOST=""; NODE_PORT=""; NODE_SNI=""; NODE_BRUTAL=0
    if [[ ! "$uri" =~ ^mirage://([^@]+)@([^:/?]+):([0-9]+)(\?(.*))?$ ]]; then
        return 1
    fi
    NODE_PWD=$(url_decode "${BASH_REMATCH[1]}")
    NODE_HOST="${BASH_REMATCH[2]}"
    NODE_PORT="${BASH_REMATCH[3]}"
    local query="${BASH_REMATCH[5]:-}"
    local IFS='&'; local pairs=($query); unset IFS
    for p in "${pairs[@]}"; do
        local k="${p%%=*}" v="${p#*=}"
        case "$k" in
            sni)    NODE_SNI=$(url_decode "$v") ;;
            brutal) NODE_BRUTAL="$v" ;;
        esac
    done
}

# ──────────────────────────────────────────────────────────────────────────────
# 客户端多节点收集（push 一个节点到 NODES_* 数组）
# ──────────────────────────────────────────────────────────────────────────────

# 调用前由 install_client 重置 NODES_HOST 等数组。每次调用收集一个节点，
# 优先让用户粘 mirage:// URI；URI 无效或留空时回退手动逐项。
_collect_node() {
    local idx=$(( ${#NODES_HOST[@]} + 1 ))
    local src_choice
    src_choice=$(ask_choice "节点 #${idx} 输入方式" \
        "粘贴 mirage:// 节点串（推荐）" \
        "手动逐项输入")
    local h="" p="" pw="" sni="" brutal=0
    if [[ "$src_choice" == "1" ]]; then
        while :; do
            local uri_in
            uri_in=$(ask "请粘贴 mirage:// 节点串" "")
            if [[ -z "$uri_in" ]]; then
                warn "未输入，退回手动逐项输入"
                src_choice="2"; break
            fi
            if parse_node_uri "$uri_in"; then
                h="$NODE_HOST"; p="$NODE_PORT"; pw="$NODE_PWD"; sni="$NODE_SNI"
                brutal="${NODE_BRUTAL:-0}"
                ok "节点 #${idx} 解析成功：${h}:${p} (SNI=${sni})"
                break
            else
                warn "节点格式无效，期望：mirage://<密码>@<host>:<port>?sni=<域名>"
            fi
        done
    fi
    if [[ -z "$h" ]]; then
        h=$(ask "节点 #${idx} 服务端地址（IP 或域名）" "")
        [[ -z "$h" ]] && err "服务端地址不能为空"
        p=$(ask_port "节点 #${idx} 服务端端口" "443")
        pw=$(ask "节点 #${idx} 密码（与服务端一致；明文显示以便确认）" "")
        [[ -z "$pw" ]] && err "密码不能为空"
        sni=$(ask "节点 #${idx} 伪装 SNI（与服务端一致）" "www.apple.com")
    fi
    NODES_HOST+=("$h")
    NODES_PORT+=("$p")
    NODES_PASSWORD+=("$pw")
    NODES_SNI+=("$sni")
    NODES_BRUTAL+=("$brutal")
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
        val=$(ask "伪装 SNI（mirage 会从该域名拉真实 TLS 握手做反指纹）" "$default")
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
    ask_yn "未检测到 Brutal。需要为本机安装吗？（自建 VPS 推荐）" n || return 1

    # 跑官方一键脚本（https://github.com/apernet/tcp-brutal）
    info "下载并运行官方安装脚本：curl -fsSL https://tcp.hy2.sh/ | bash"
    if ! command -v curl &>/dev/null; then
        case $PKG_MGR in
            apt) apt-get install -y curl ;;
            dnf|yum) "$PKG_MGR" install -y curl ;;
            apk) apk add --no-cache curl ;;
            *) warn "需要 curl，请手装后重试"; return 1 ;;
        esac
    fi
    # 真装。失败不致命（用户可能在不支持的内核上）
    if curl -fsSL https://tcp.hy2.sh/ | bash >&2; then
        info "安装脚本跑完，校验内核模块..."
        # tcp.hy2.sh 跑 modprobe 后 brutal 应在 tcp_available_congestion_control 里
        if brutal_loaded; then
            ok "Brutal 内核模块装好并已加载"
            return 0
        fi
        warn "安装脚本退出 0，但 modprobe 后 brutal 不在 tcp_available_congestion_control 里"
        warn "可能：内核版本不匹配 / 重启后才生效 / 模块路径异常"
        warn "排查：modinfo brutal、dmesg | tail、sysctl net.ipv4.tcp_available_congestion_control"
        ask_yn "继续不启用 Brutal（推荐）？" y && return 1
    else
        warn "官方安装脚本失败（network / 内核不支持 / 编译错误）"
        warn "参考：https://github.com/apernet/tcp-brutal#installation"
    fi
    return 1
}

# ──────────────────────────────────────────────────────────────────────────────
# 日志配置（cfg.log.format + 可选 logrotate）
# ──────────────────────────────────────────────────────────────────────────────

# 读已有 config_client.json，提取连接信息 + 当前分流 / DNS / API 状态
# 成功返回 0，并设置全局 EXISTING_* 变量
_load_existing_client_cfg() {
    local path=$1
    [[ -f "$path" ]] || return 1
    local data
    data=$(python3 - "$path" <<'PY' 2>/dev/null
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
# 找 mirage outbound（兼容 legacy "pyrealiy" type）
mirage = next((o for o in c.get("outbounds", [])
               if o.get("type") in ("mirage", "pyrealiy")), None)
if not mirage:
    sys.exit(1)
print(mirage.get("server") or mirage.get("server_host", ""))
print(mirage.get("server_port", 443))
print(mirage.get("password", ""))
print(mirage.get("sni") or mirage.get("camouflage_host", ""))
# socks5 / mixed listen port
ib = (c.get("inbounds") or [{}])[0]
listen = ib.get("listen", "")
port = listen.rsplit(":", 1)[-1] if ":" in listen else ""
print(port)
# 路由规则数
rules = c.get("route", {}).get("rules") or []
print(len(rules))
# DNS / API 状态
dns_listen = c.get("dns", {}).get("listen", "")
if not dns_listen and c.get("dns_listen_port"):
    dns_listen = f"{c.get('dns_listen_host', '127.0.0.1')}:{c['dns_listen_port']}"
print(dns_listen)
print((c.get("api") or {}).get("listen", ""))
PY
    ) || return 1
    [[ -z "$data" ]] && return 1
    EXISTING_SERVER_HOST=$(echo "$data" | sed -n '1p')
    EXISTING_SERVER_PORT=$(echo "$data" | sed -n '2p')
    EXISTING_PASSWORD=$(echo "$data" | sed -n '3p')
    EXISTING_CAMOUFLAGE=$(echo "$data" | sed -n '4p')
    EXISTING_SOCKS5_PORT=$(echo "$data" | sed -n '5p')
    EXISTING_RULES_COUNT=$(echo "$data" | sed -n '6p')
    EXISTING_DNS_LISTEN=$(echo "$data" | sed -n '7p')
    EXISTING_API_LISTEN=$(echo "$data" | sed -n '8p')
    [[ -n "$EXISTING_SERVER_HOST" ]]
}

# DNS 详细配置：监听地址 / 端口 / 自定义上游
ask_dns_details() {
    info "DNS 转发器详细配置"
    DNS_LISTEN_HOST=$(ask "DNS 监听地址（0.0.0.0 = 允许 LAN 设备查询）" "127.0.0.1")
    DNS_LISTEN_PORT=$(ask_port "DNS 监听端口（5353 无需 root；53 需 root 或 cap_net_bind_service）" "5353" udp)
    if ask_yn "自定义上游 DNS 服务器（否则用预设：cn=119.29.29.29 远端=1.1.1.1:53）？" n; then
        case "$dns_preset" in
            china_split)
                DNS_CN=$(ask "国内域名上游（命中 direct 出口时用，纯 UDP）" "119.29.29.29")
                DNS_REMOTE=$(ask "国外域名上游（host:port / tls://host:853 / https://1.1.1.1/dns-query）" "1.1.1.1:53")
                ;;
            full_proxy)
                DNS_REMOTE=$(ask "上游 DNS（全代理；host:port / tls:// / https://）" "1.1.1.1:53")
                DNS_CN="$DNS_REMOTE"
                ;;
        esac
    fi
}

# 自定义高级路由：逐项配置 geo tag 归属
ROUTE_ADV_RULES=()
ROUTE_ADV_DEFAULT="proxy"
ask_route_advanced() {
    info "高级路由：逐项配置常用 geo tag"
    info "每项 4 选 1：0=跳过 / 1=direct（直连）/ 2=proxy / 3=block"
    ROUTE_ADV_RULES=()
    # 内网总是 direct（必须）
    ROUTE_ADV_RULES+=('{"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"], "outbound": "direct"}')
    info "（内网 / 保留地址已固定 direct，不可关）"

    _ask_rule "广告 (category-ads-all)"          "geosite" "loyalsoldier:category-ads-all"   "3"
    _ask_rule "国内域名 (geosite:cn)"            "geosite" "loyalsoldier:cn"                  "1"
    _ask_rule "国内 IP (geoip:cn)"               "geoip"   "loyalsoldier:cn"                  "1"
    _ask_rule "Apple 中国 (apple-cn)"            "geosite" "loyalsoldier:apple-cn"            "1"
    _ask_rule "Google 中国 (google-cn)"          "geosite" "loyalsoldier:google-cn"           "1"
    _ask_rule "Microsoft 中国 (microsoft-cn)"    "geosite" "loyalsoldier:microsoft-cn"        "1"
    _ask_rule "GFW 黑名单 (geosite:gfw)"         "geosite" "loyalsoldier:gfw"                 "2"
    _ask_rule "海外位置 (geolocation-!cn)"       "geosite" "loyalsoldier:geolocation-!cn"     "2"
    _ask_rule "Netflix"                           "geosite" "loyalsoldier:netflix"             "0"
    _ask_rule "Disney+"                           "geosite" "loyalsoldier:disney"              "0"
    _ask_rule "YouTube"                           "geosite" "loyalsoldier:youtube"             "0"
    _ask_rule "OpenAI / ChatGPT"                  "geosite" "loyalsoldier:openai"              "0"
    _ask_rule "Telegram"                          "geosite" "loyalsoldier:telegram"            "0"

    echo
    local d
    d=$(ask "默认出口（未命中规则时；1=direct / 2=proxy）" "2")
    case "$d" in
        1) ROUTE_ADV_DEFAULT="direct" ;;
        *) ROUTE_ADV_DEFAULT="proxy" ;;
    esac
    info "已配 $(( ${#ROUTE_ADV_RULES[@]} )) 条规则，默认出口：$ROUTE_ADV_DEFAULT"
}

# _ask_rule "显示名" "geosite|geoip" "tag" "默认 0-3"
_ask_rule() {
    local label=$1 type=$2 tag=$3 default=$4
    # router 用 sing-box 风格的字段名：rule_set（不是 geosite）
    local field=$type
    [[ "$field" == "geosite" ]] && field="rule_set"
    local v
    v=$(ask "  ${label}" "$default")
    case "$v" in
        0|"") ;;
        1) ROUTE_ADV_RULES+=("{\"${field}\": [\"${tag}\"], \"outbound\": \"direct\"}") ;;
        2) ROUTE_ADV_RULES+=("{\"${field}\": [\"${tag}\"], \"outbound\": \"proxy\"}") ;;
        3) ROUTE_ADV_RULES+=("{\"${field}\": [\"${tag}\"], \"outbound\": \"block\"}") ;;
        *) warn "无效输入 '$v'，跳过 $label"; ;;
    esac
}

ask_log_config() {
    info "日志配置"
    # 格式
    local fmt_choice
    fmt_choice=$(ask_choice "日志格式" \
        "text — 人类可读（默认）" \
        "json — 每行一个 JSON，方便 Loki / ELK / jq 解析")
    case $fmt_choice in
        1) LOG_FORMAT="text" ;;
        2) LOG_FORMAT="json" ;;
    esac
    info "已选：${LOG_FORMAT}"

    # 最小级别
    local lvl_choice
    lvl_choice=$(ask_choice "最小日志级别（低于此级别的日志被丢弃）" \
        "INFO     — 关键事件 + 警告 + 错误（推荐）" \
        "DEBUG    — 包含调试细节（量大；排查问题时用）" \
        "WARNING  — 仅警告 + 错误" \
        "ERROR    — 仅错误")
    case $lvl_choice in
        1) LOG_LEVEL="INFO" ;;
        2) LOG_LEVEL="DEBUG" ;;
        3) LOG_LEVEL="WARNING" ;;
        4) LOG_LEVEL="ERROR" ;;
    esac
    info "已选：${LOG_LEVEL}"

    # 进程内文件日志 + 轮转：仅 system 模式默认开（inplace 让日志走 stderr/项目目录）
    if [[ "$INSTALL_MODE" != "system" ]]; then
        LOGROTATE_ENABLED="no"
        info "inplace 模式：日志走 stderr / systemd append（开发用，无需轮转）"
        return
    fi

    if ask_yn "启用进程内文件日志 + 自动轮转？（推荐）" y; then
        LOGROTATE_ENABLED="yes"
        LOGROTATE_MAXSIZE_HUMAN=$(ask "单文件最大体积（K / M / G 后缀）" "100M")
        LOGROTATE_MAXSIZE=$(_to_bytes "$LOGROTATE_MAXSIZE_HUMAN")
        LOGROTATE_KEEP=$(ask "保留几份历史（达到后丢最老的）" "7")
        if ask_yn "压缩历史日志（gzip）？" y; then
            LOGROTATE_COMPRESS="true"
        else
            LOGROTATE_COMPRESS="false"
        fi
        info "进程内轮转：超 ${LOGROTATE_MAXSIZE_HUMAN}（${LOGROTATE_MAXSIZE} B）即转，保留 ${LOGROTATE_KEEP} 份，压缩=${LOGROTATE_COMPRESS}"
    else
        LOGROTATE_ENABLED="no"
        warn "未启用文件日志：日志走 stderr，systemd journal 捕获"
    fi
}

# 生成 cfg.log JSON 片段：format + 可选 file/max_bytes/backup_count/compress
_build_log_block() {
    local kind=$1   # server 或 client
    if [[ "$LOGROTATE_ENABLED" == "yes" ]]; then
        local log_path="${EFFECTIVE_LOG_DIR}/${kind}.log"
        cat <<JSON
"log": {
        "format":       "${LOG_FORMAT}",
        "file":         "${log_path}",
        "max_bytes":    ${LOGROTATE_MAXSIZE},
        "backup_count": ${LOGROTATE_KEEP},
        "compress":     ${LOGROTATE_COMPRESS}
    },
    "log_levels": {"default": "${LOG_LEVEL}"}
JSON
    else
        echo "\"log\": {\"format\": \"${LOG_FORMAT}\"}, \"log_levels\": {\"default\": \"${LOG_LEVEL}\"}"
    fi
}

# "100M" → 104857600 等。支持 K/M/G 后缀（不区分大小写），无后缀 = 直接字节
_to_bytes() {
    local s=$1
    local n unit
    n=$(echo "$s" | grep -oE "^[0-9]+")
    unit=$(echo "${s:${#n}}" | tr '[:lower:]' '[:upper:]')
    case "$unit" in
        ""|B)   echo "$n" ;;
        K|KB)   echo $(( n * 1024 )) ;;
        M|MB)   echo $(( n * 1024 * 1024 )) ;;
        G|GB)   echo $(( n * 1024 * 1024 * 1024 )) ;;
        *)      echo "$n" ;;  # 不识别就当字节
    esac
}

# ──────────────────────────────────────────────────────────────────────────────
# 安装模式（FHS 系统 vs 原地）+ 路径解析
# ──────────────────────────────────────────────────────────────────────────────

choose_install_mode() {
    # 已在 /opt/mirage 跑 → 自动 system
    if [[ "$WORK_DIR" == "$INSTALL_PREFIX" ]]; then
        INSTALL_MODE="system"
        info "工作目录已是 $INSTALL_PREFIX，自动 system 模式"
        return
    fi
    info "安装位置 2 选 1："
    info "  system  — 文件分散到 /opt /etc /var/log /var/lib（推荐，FHS 规范）"
    info "  inplace — 全部留在当前项目目录（开发 / 测试用）"
    local c
    c=$(ask_choice "请选择" \
        "系统标准位置（推荐）" \
        "原地安装（开发用）")
    case $c in
        1) INSTALL_MODE="system" ;;
        2) INSTALL_MODE="inplace" ;;
    esac
}

resolve_paths() {
    if [[ "$INSTALL_MODE" == "system" ]]; then
        EFFECTIVE_LIB="$INSTALL_PREFIX"
        EFFECTIVE_ETC="$ETC_DIR"
        EFFECTIVE_LOG_DIR="$LOG_DIR"
        EFFECTIVE_GEOSITE_DIR="$STATE_DIR/geosite"
    else
        EFFECTIVE_LIB="$WORK_DIR"
        EFFECTIVE_ETC="$WORK_DIR"
        EFFECTIVE_LOG_DIR="$WORK_DIR"
        EFFECTIVE_GEOSITE_DIR=".geosite"   # 相对 WorkingDirectory
    fi
    info "安装目标："
    info "  程序  → $EFFECTIVE_LIB"
    info "  配置  → $EFFECTIVE_ETC"
    info "  日志  → $EFFECTIVE_LOG_DIR"
    info "  状态  → $EFFECTIVE_GEOSITE_DIR"
}

install_system_files() {
    [[ "$INSTALL_MODE" == "system" ]] || return 0
    info "复制源文件到 $INSTALL_PREFIX ..."
    mkdir -p "$INSTALL_PREFIX" "$ETC_DIR" "$LOG_DIR" "$STATE_DIR"
    chmod 755 "$INSTALL_PREFIX" "$ETC_DIR" "$LOG_DIR" "$STATE_DIR"

    if command -v rsync &>/dev/null; then
        rsync -a --delete \
              --exclude='__pycache__' --exclude='.git' --exclude='.venv' \
              --exclude='*.log' --exclude='config_server.json' --exclude='config_client.json' \
              --exclude='.geosite' --exclude='config_*.example.jsonc' \
              "$WORK_DIR/server.py" "$WORK_DIR/client.py" "$WORK_DIR/install.sh" \
              "$WORK_DIR/bench.py" "$WORK_DIR/requirements.txt" "$WORK_DIR/core/" \
              "$INSTALL_PREFIX/"
        # core/ 单独同步（rsync 上面只 copy 文件，目录用单独 call）
        rsync -a --delete \
              --exclude='__pycache__' \
              "$WORK_DIR/core/" "$INSTALL_PREFIX/core/"
        # 示例文件也复制一份（用户参考）
        cp -f "$WORK_DIR"/config_*.example.jsonc "$INSTALL_PREFIX/" 2>/dev/null || true
    else
        # 兜底：cp -r
        cp -f "$WORK_DIR/server.py" "$WORK_DIR/client.py" "$WORK_DIR/install.sh" \
              "$WORK_DIR/bench.py" "$WORK_DIR/requirements.txt" "$INSTALL_PREFIX/"
        rm -rf "$INSTALL_PREFIX/core"
        cp -rf "$WORK_DIR/core" "$INSTALL_PREFIX/"
        cp -f "$WORK_DIR"/config_*.example.jsonc "$INSTALL_PREFIX/" 2>/dev/null || true
    fi
    chmod 755 "$INSTALL_PREFIX/install.sh"
    ok "已复制到 $INSTALL_PREFIX"
}

install_shim_scripts() {
    [[ "$INSTALL_MODE" == "system" ]] || return 0
    local role=$1   # server or client
    local shim="$BIN_DIR/mirage-${role}"
    cat > "$shim" <<EOF
#!/bin/bash
# Mirage ${role^} CLI shim（由 install.sh 生成）
exec /usr/bin/python3 ${INSTALL_PREFIX}/${role}.py "\${@:-${ETC_DIR}/config_${role}.json}"
EOF
    chmod 755 "$shim"
    ok "已建 shim：$shim"
}

# ──────────────────────────────────────────────────────────────────────────────
# 写配置文件
# ──────────────────────────────────────────────────────────────────────────────

write_server_config() {
    local listen_host=$1 listen_port=$2 password=$3 camouflage=$4 brutal_rate_bps=$5
    # 注意：服务端目前仍用老平铺 schema（不写 schema_version 字段）。
    # 写 schema_version=1 + 老顶层字段会触发 load_config 的"unknown top-level keys" WARN。
    # 服务端的 schema_v1 化是未来工作，schema_v0 → v1 自动识别仍由 _LEGACY_TOP_KEYS 覆盖。
    mkdir -p "$EFFECTIVE_ETC"
    local path="$EFFECTIVE_ETC/config_server.json"
    local log_block
    log_block=$(_build_log_block "server")
    cat > "$path" <<EOF
{
    "listen_host": "${listen_host}",
    "listen_port": ${listen_port},
    "password": "${password}",
    "camouflage_host": "${camouflage}",
    "brutal_rate_bps": ${brutal_rate_bps},
    ${log_block}
}
EOF
    chmod 600 "$path"
    ok "已生成 $path"
}

write_client_config() {
    local listen_host=$1 socks5_port=$2 routing_preset=$3 dns_preset=$4
    local tproxy_port=$5 enable_api=$6 api_secret=$7
    # 节点信息来自全局 NODES_* 数组；GROUP_TYPE 非空 → 多节点 + 组节点 'proxy'
    local routing_json dns_extra api_extra tproxy_extra outbounds_block

    case $routing_preset in
        transparent) routing_json='{"default": "proxy", "rules": []}' ;;
        china_split)
            routing_json='{
            "default": "proxy",
            "rules": [
                {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"], "outbound": "direct"},
                {"rule_set": ["loyalsoldier:cn"], "outbound": "direct"},
                {"geoip":    ["loyalsoldier:cn"], "outbound": "direct"}
            ]
        }'
            ;;
        advanced)
            # 把 ROUTE_ADV_RULES 数组拼成 JSON 数组
            local rules_inner=""
            local r
            for r in "${ROUTE_ADV_RULES[@]}"; do
                rules_inner+="
                ${r},"
            done
            rules_inner="${rules_inner%,}"   # 去尾逗号
            routing_json="{
            \"default\": \"${ROUTE_ADV_DEFAULT}\",
            \"rules\": [${rules_inner}
            ]
        }"
            ;;
        custom) routing_json='{"default": "proxy", "rules": []}' ;;
    esac

    # DNS section：使用 ask_dns_details 的值（带兜底默认）
    case $dns_preset in
        off)
            dns_extra=""
            ;;
        china_split|full_proxy)
            local cn_val remote_val
            if [[ "$dns_preset" == "full_proxy" ]]; then
                cn_val="${DNS_REMOTE:-1.1.1.1:53}"
                remote_val="${DNS_REMOTE:-1.1.1.1:53}"
            else
                cn_val="${DNS_CN:-119.29.29.29}"
                remote_val="${DNS_REMOTE:-1.1.1.1:53}"
            fi
            # 0.4.41+：所有 DNS 字段挪进 cfg.dns 块（schema_v1 合规）；
            # listen / cn / remote 由 config.py 自动投射到顶层旧 key
            dns_extra=',
    "dns": {
        "listen": "'"${DNS_LISTEN_HOST:-127.0.0.1}"':'"${DNS_LISTEN_PORT:-5353}"'",
        "cn":     "'"${cn_val}"'",
        "remote": "'"${remote_val}"'"
    }'
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

    if (( tproxy_port > 0 )); then
        tproxy_extra=',
    "tproxy_port": '"$tproxy_port"
    else
        tproxy_extra=""
    fi

    # geosite 缓存目录：system 模式写绝对路径 /var/lib/mirage/geosite；
    # inplace 模式留相对 ".geosite"（相对 systemd WorkingDirectory）
    local geosite_extra=""
    if [[ "$INSTALL_MODE" == "system" ]]; then
        geosite_extra=",
    \"geosite_dir\": \"${EFFECTIVE_GEOSITE_DIR}\""
    fi

    # geosite/geoip 下载源：没有这两个字段 ensure_all() 就不会下任何文件，
    # rule_set / geoip 规则会被 router skip。loyalsoldier 同时提供 site + ip
    local geo_sources_extra=',
    "geosite_sources": [
        {"name": "loyalsoldier",
         "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"}
    ],
    "geoip_sources": [
        {"name": "loyalsoldier",
         "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"}
    ]'

    # 构造 outbounds 块：单节点 → 1 个 mirage（tag=proxy），多节点 → N 个 mirage(tag=proxy-i) + 1 个组(tag=proxy)
    local node_count=${#NODES_HOST[@]}
    if (( node_count <= 1 )); then
        local brutal_extra=""
        if [[ "${NODES_BRUTAL[0]:-0}" =~ ^[0-9]+$ ]] && (( ${NODES_BRUTAL[0]:-0} > 0 )); then
            brutal_extra=",
            \"brutal_rate_bps\": ${NODES_BRUTAL[0]}"
        fi
        outbounds_block="{
            \"tag\": \"proxy\",
            \"type\": \"mirage\",
            \"server_host\": \"${NODES_HOST[0]}\",
            \"server_port\": ${NODES_PORT[0]},
            \"password\": \"${NODES_PASSWORD[0]}\",
            \"camouflage_host\": \"${NODES_SNI[0]}\"${brutal_extra}
        },
        {\"tag\": \"direct\", \"type\": \"direct\"},
        {\"tag\": \"block\",  \"type\": \"block\"}"
    else
        local i child_tags=""
        outbounds_block=""
        for (( i = 0; i < node_count; i++ )); do
            local brutal_extra=""
            if [[ "${NODES_BRUTAL[i]:-0}" =~ ^[0-9]+$ ]] && (( ${NODES_BRUTAL[i]:-0} > 0 )); then
                brutal_extra=",
            \"brutal_rate_bps\": ${NODES_BRUTAL[i]}"
            fi
            outbounds_block+="{
            \"tag\": \"proxy-$((i+1))\",
            \"type\": \"mirage\",
            \"server_host\": \"${NODES_HOST[i]}\",
            \"server_port\": ${NODES_PORT[i]},
            \"password\": \"${NODES_PASSWORD[i]}\",
            \"camouflage_host\": \"${NODES_SNI[i]}\"${brutal_extra}
        },
        "
            [[ -n "$child_tags" ]] && child_tags+=", "
            child_tags+="\"proxy-$((i+1))\""
        done
        local group_extra=""
        if [[ "$GROUP_TYPE" == "urltest" ]]; then
            group_extra=", \"tolerance\": 50"
        elif [[ "$GROUP_TYPE" == "selector" ]]; then
            group_extra=", \"default\": \"proxy-1\""
        fi
        outbounds_block+="{
            \"tag\": \"proxy\",
            \"type\": \"${GROUP_TYPE}\",
            \"outbounds\": [${child_tags}]${group_extra}
        },
        {\"tag\": \"direct\", \"type\": \"direct\"},
        {\"tag\": \"block\",  \"type\": \"block\"}"
    fi

    mkdir -p "$EFFECTIVE_ETC"
    local path="$EFFECTIVE_ETC/config_client.json"
    cat > "$path" <<EOF
{
    "schema_version": 1,
    "inbounds": [
        {"type": "mixed", "listen": "${listen_host}:${socks5_port}"}
    ],
    "outbounds": [
        ${outbounds_block}
    ],
    "route": ${routing_json},
    $(_build_log_block "client")${dns_extra}${api_extra}${tproxy_extra}${geosite_extra}${geo_sources_extra}
}
EOF
    chmod 600 "$path"
    ok "已生成 $path"
    if [[ "$routing_preset" == "custom" ]]; then
        warn "你选择了自定义路由：请编辑 $path 的 route.rules 数组"
        warn "字段格式见 README 的「分流规则」一节"
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
    local unit="/etc/systemd/system/mirage-${kind}.service"
    local log_path="${EFFECTIVE_LOG_DIR}/${kind}.log"
    local cfg_path="${EFFECTIVE_ETC}/config_${kind}.json"
    mkdir -p "$EFFECTIVE_LOG_DIR"
    info "写 systemd unit：$unit"
    cat > "$unit" <<EOF
[Unit]
Description=Mirage ${kind^}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${EFFECTIVE_LIB}
ExecStart=/usr/bin/python3 ${EFFECTIVE_LIB}/${kind}.py ${cfg_path}
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=3
$( if [[ "$LOGROTATE_ENABLED" == "yes" ]]; then
    # 进程内写文件，systemd 只兜底捕获 early/crash 到 journal
    echo "StandardOutput=journal"
    echo "StandardError=journal"
else
    echo "StandardOutput=append:${log_path}"
    echo "StandardError=append:${log_path}"
fi )
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "mirage-${kind}.service" >/dev/null
    ok "mirage-${kind}.service 已 enable（开机自启）"
    if ask_yn "现在启动 mirage-${kind}.service 吗？" y; then
        systemctl restart "mirage-${kind}.service"
        sleep 2
        if systemctl is-active --quiet "mirage-${kind}.service"; then
            ok "mirage-${kind}.service 已运行"
            info "查看状态：systemctl status mirage-${kind}"
            info "查看日志：tail -f ${log_path}"
            info "重载配置：systemctl reload mirage-${kind}"
        else
            warn "服务启动失败。查日志：tail -50 ${log_path}"
        fi
    fi
}

# ── OpenRC（Alpine / Gentoo）──
_install_openrc() {
    local kind=$1
    local unit="/etc/init.d/mirage-${kind}"
    local log_path="${EFFECTIVE_LOG_DIR}/${kind}.log"
    local cfg_path="${EFFECTIVE_ETC}/config_${kind}.json"
    mkdir -p "$EFFECTIVE_LOG_DIR"
    info "写 OpenRC service：$unit"
    cat > "$unit" <<EOF
#!/sbin/openrc-run
# Mirage ${kind^} OpenRC service

name="Mirage ${kind^}"
description="Mirage ${kind^} service"

command="/usr/bin/python3"
command_args="${EFFECTIVE_LIB}/${kind}.py ${cfg_path}"
command_background=true
pidfile="/run/mirage-${kind}.pid"
$( if [[ "$LOGROTATE_ENABLED" == "yes" ]]; then
    echo "output_log=\"/dev/null\""
    echo "error_log=\"/dev/null\""
else
    echo "output_log=\"${log_path}\""
    echo "error_log=\"${log_path}\""
fi )
directory="${EFFECTIVE_LIB}"

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
    rc-update add "mirage-${kind}" default >/dev/null
    ok "mirage-${kind} 已加入 default runlevel（开机自启）"
    if ask_yn "现在启动 mirage-${kind} 吗？" y; then
        rc-service "mirage-${kind}" restart >/dev/null 2>&1 || \
        rc-service "mirage-${kind}" start
        sleep 2
        if rc-service "mirage-${kind}" status &>/dev/null; then
            ok "mirage-${kind} 已运行"
            info "查看状态：rc-service mirage-${kind} status"
            info "查看日志：tail -f ${log_path}"
            info "重载配置：rc-service mirage-${kind} reload"
        else
            warn "服务启动失败。查日志：tail -50 ${log_path}"
        fi
    fi
}

# ── SysV init.d ──
_install_sysv() {
    local kind=$1
    local unit="/etc/init.d/mirage-${kind}"
    local log_path="${EFFECTIVE_LOG_DIR}/${kind}.log"
    local cfg_path="${EFFECTIVE_ETC}/config_${kind}.json"
    mkdir -p "$EFFECTIVE_LOG_DIR"
    info "写 SysV init script：$unit"
    cat > "$unit" <<EOF
#!/bin/bash
### BEGIN INIT INFO
# Provides:          mirage-${kind}
# Required-Start:    \$network \$remote_fs
# Required-Stop:     \$network \$remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Mirage ${kind^}
# Description:       Mirage ${kind^} (TLS-camouflage proxy)
### END INIT INFO

NAME=mirage-${kind}
DAEMON=/usr/bin/python3
DAEMON_ARGS="${EFFECTIVE_LIB}/${kind}.py ${cfg_path}"
WORKDIR="${EFFECTIVE_LIB}"
PIDFILE=/var/run/\$NAME.pid
LOGFILE=${log_path}

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
$( if [[ "$LOGROTATE_ENABLED" == "yes" ]]; then
    echo "        nohup \$DAEMON \$DAEMON_ARGS > /dev/null 2>&1 &"
else
    echo "        nohup \$DAEMON \$DAEMON_ARGS >> \"\$LOGFILE\" 2>&1 &"
fi )
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
        update-rc.d "mirage-${kind}" defaults >/dev/null
    elif command -v chkconfig &>/dev/null; then
        chkconfig --add "mirage-${kind}" 2>/dev/null || true
        chkconfig "mirage-${kind}" on
    else
        warn "未找到 update-rc.d / chkconfig；不自动 enable，手动加 runlevel symlink"
    fi
    ok "mirage-${kind} 已 enable（开机自启）"
    if ask_yn "现在启动 mirage-${kind} 吗？" y; then
        service "mirage-${kind}" start
        sleep 2
        if service "mirage-${kind}" status >/dev/null; then
            ok "mirage-${kind} 已运行"
            info "查看状态：service mirage-${kind} status"
            info "查看日志：tail -f ${log_path}"
            info "重载配置：service mirage-${kind} reload"
        else
            warn "服务启动失败。查日志：tail -50 ${log_path}"
        fi
    fi
}

# ── 兜底：未知 init ──
_install_skip() {
    local kind=$1
    local cfg_path="${EFFECTIVE_ETC}/config_${kind}.json"
    warn "未识别 init 系统（既无 systemd / openrc / sysv 工具）；跳过 service 安装。"
    warn "手动启动：cd ${EFFECTIVE_LIB} && python3 ${kind}.py ${cfg_path}"
    warn "热加载配置：kill -HUP \$(pgrep -f \"python3.*${kind}.py\")"
    return 0
}

# ── 导出客户端模板与连接信息 ──
_print_and_save_client_template() {
    local server_ip=$1 port=$2 password=$3 camouflage=$4
    local node_uri
    node_uri=$(build_node_uri "$password" "$server_ip" "$port" "$camouflage" 0)
    local sys_dir="${EFFECTIVE_ETC:-/etc/mirage}"
    mkdir -p "$sys_dir" 2>/dev/null || true
    local sys_txt="${sys_dir}/mirage_client_info.txt"
    local cur_txt="mirage_client_info.txt"

    # 构造导出的文本内容
    local content
    content=$(cat <<EOF
================================================================================
Mirage 客户端连接配置信息与导出模板
================================================================================

【专属节点分享链接 (推荐)】直接复制单行链接，在客户端执行 ./install.sh 引导时一键导入：
    $node_uri

--------------------------------------------------------------------------------
【快捷填参连接字段】也可手动依次填入：
    服务端地址  : $server_ip
    服务端端口  : $port
    密码        : $password
    伪装 SNI    : $camouflage

--------------------------------------------------------------------------------
【客户端完整配置模板】也可直接将下方 JSON 保存为客户端机器的 config_client.json，
 放入客户端 /etc/mirage/ 目录或在客户端执行 install.sh 时自动识别加载：

{
    "schema_version": 1,
    "inbounds": [{"type": "mixed", "listen": "127.0.0.1:7890"}],
    "outbounds": [
        {
            "tag": "proxy", "type": "mirage",
            "server_host": "$server_ip",
            "server_port": $port,
            "password": "$password",
            "camouflage_host": "$camouflage"
        },
        {"tag": "direct", "type": "direct"},
        {"tag": "block",  "type": "block"}
    ],
    "route": {
        "default": "proxy",
        "rules": [
            {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"], "outbound": "direct"},
            {"rule_set": ["loyalsoldier:cn"], "outbound": "direct"},
            {"geoip":    ["loyalsoldier:cn"], "outbound": "direct"}
        ]
    },
    "dns": {
        "listen": "127.0.0.1:5353",
        "cn":     "119.29.29.29",
        "remote": "1.1.1.1:53"
    },
    "geosite_sources": [
        {"name": "loyalsoldier",
         "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"}
    ],
    "geoip_sources": [
        {"name": "loyalsoldier",
         "url":  "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"}
    ]
}
================================================================================
EOF
)

    # 写入文件
    echo "$content" > "$sys_txt" 2>/dev/null && chmod 600 "$sys_txt" 2>/dev/null || true
    if [[ "$(realpath "$sys_txt" 2>/dev/null || echo "$sys_txt")" != "$(realpath "$cur_txt" 2>/dev/null || echo "$cur_txt")" ]]; then
        echo "$content" > "$cur_txt" 2>/dev/null && chmod 600 "$cur_txt" 2>/dev/null || true
    fi

    # 终端输出屏幕提示
    title "客户端配置模板与连接字段"
    cat <<EOF
$(_c 32 "【一键节点分享链接 (推荐)】在客户端安装时选择粘贴即可秒装：")
    $(_c 33 "$node_uri")

$(_c 36 "【手动填参字段】")
    服务端地址  : $(_c 33 "$server_ip")
    服务端端口  : $(_c 33 "$port")
    密码        : $(_c 33 "$password")
    伪装 SNI    : $(_c 33 "$camouflage")

$(_c 36 "或者直接把导出的 config_client.json 模板拷到客户端（含国内外分流 + DNS）：")

EOF
    echo "$content" | sed -n '/^{/,/^}/p'
    echo

    ok "配置信息与模板已成功导出至文本文件："
    [[ -f "$sys_txt" ]] && info "  系统路径：$sys_txt"
    [[ -f "$cur_txt" && "$(realpath "$cur_txt" 2>/dev/null)" != "$(realpath "$sys_txt" 2>/dev/null)" ]] && info "  当前目录：$(pwd)/$cur_txt"
    echo
    $(_c 36 "提示：传到客户端建议用 scp 或 SFTP（更安全），不要走明文聊天软件；")
    $(_c 36 "       后续随时在服务端执行 ./install.sh info 或通过菜单选项 4 再次查看与导出。")
}

export_server_info() {
    title "查看与导出现有服务端配置信息"
    local cfg_file=""
    local etc_target="/etc/mirage"
    for p in "/etc/mirage/config_server.json" "/etc/pyrealiy/config_server.json" "./config_server.json"; do
        if [[ -f "$p" ]]; then
            cfg_file="$p"
            if [[ "$p" == "./config_server.json" ]]; then
                etc_target="."
            fi
            break
        fi
    done

    if [[ -z "$cfg_file" ]]; then
        err "未找到服务端的配置文件 config_server.json。请确认本机是否已安装服务端。"
    fi

    info "正在从 $cfg_file 解析连接参数..."
    local port pwd cam
    port=$(python3 -c "import json, sys; print(json.load(open(sys.argv[1])).get('listen_port', 443))" "$cfg_file" 2>/dev/null)
    pwd=$(python3 -c "import json, sys; print(json.load(open(sys.argv[1])).get('password', ''))" "$cfg_file" 2>/dev/null)
    cam=$(python3 -c "import json, sys; print(json.load(open(sys.argv[1])).get('camouflage_host', 'www.apple.com'))" "$cfg_file" 2>/dev/null)

    if [[ -z "$pwd" ]]; then
        err "从 $cfg_file 中解析密码失败！请检查 JSON 格式。"
    fi

    local auto_ip
    auto_ip=$(curl -s -4 --max-time 5 https://api.ipify.org 2>/dev/null || curl -s -4 --max-time 5 https://ifconfig.me 2>/dev/null || echo "")
    local server_ip
    server_ip=$(ask "确认服务端公网 IP 或域名（用于生成客户端配置）" "${auto_ip:-1.2.3.4}")

    EFFECTIVE_ETC="$etc_target" _print_and_save_client_template "$server_ip" "$port" "$pwd" "$cam"
}

# ──────────────────────────────────────────────────────────────────────────────
# 服务端流程
# ──────────────────────────────────────────────────────────────────────────────

install_server() {
    title "服务端安装"

    local listen_host listen_port password camouflage brutal_rate_bps=0
    local auto_ip
    auto_ip=$(curl -s -4 --max-time 5 https://api.ipify.org 2>/dev/null || curl -s -4 --max-time 5 https://ifconfig.me 2>/dev/null || echo "")
    local public_host
    public_host=$(ask "本机公网 IP 或域名（供客户端导出连接使用）" "${auto_ip:-1.2.3.4}")
    listen_host="0.0.0.0"
    listen_port=$(ask_port "监听端口" "443")

    if ask_yn "自动生成密码？" y; then
        password=$(openssl rand -base64 24 | tr -d /+= | head -c 24)
        info "已生成密码：$password"
    else
        password=$(ask "自定义密码（明文显示以便确认）" "")
        while [[ -z "$password" ]]; do
            warn "密码不能为空"
            password=$(ask "自定义密码（明文显示以便确认）" "")
        done
    fi

    camouflage=$(ask_camouflage_host "www.apple.com")

    if handle_brutal_optional; then
        local rate_mbps
        rate_mbps=$(ask "每条连接的 Brutal 速率上限（Mbps）" "10")
        brutal_rate_bps=$(( rate_mbps * 1000 * 1000 ))
        info "Brutal 速率：${rate_mbps} Mbps / connection"
    fi

    # 记下连接信息：两端模式下客户端直接复用，无需重输
    INSTALLED_SERVER_PORT="$listen_port"
    INSTALLED_SERVER_PASSWORD="$password"
    INSTALLED_SERVER_CAMOUFLAGE="$camouflage"

    ask_log_config

    install_system_files
    install_shim_scripts "server"
    write_server_config "$listen_host" "$listen_port" "$password" "$camouflage" "$brutal_rate_bps"
    install_service_unit "server"

    # 防火墙提醒：新手最常踩的坑——服务起来了但云安全组/ufw 没放行端口 → 客户端连不上
    echo
    warn "别忘了放行入站 TCP 端口 ${listen_port}（最常见的\"连不上\"原因）："
    if command -v ufw &>/dev/null; then
        info "  本机 ufw：sudo ufw allow ${listen_port}/tcp"
    elif command -v firewall-cmd &>/dev/null; then
        info "  本机 firewalld：sudo firewall-cmd --add-port=${listen_port}/tcp --permanent && sudo firewall-cmd --reload"
    fi
    info "  云服务器还需在控制台「安全组 / 防火墙」放行该端口"

    # 两端模式：客户端马上在本机装并复用以上信息，"拷到远程客户端"那段是噪音，跳过
    if [[ "${BOTH_MODE:-0}" == "1" ]]; then
        return
    fi

    # 末尾：打印并导出客户端配置模板与连接信息
    _print_and_save_client_template "$public_host" "$listen_port" "$password" "$camouflage"
}

# ──────────────────────────────────────────────────────────────────────────────
# 客户端流程
# ──────────────────────────────────────────────────────────────────────────────

install_client() {
    title "客户端安装"

    local server_host server_port password camouflage socks5_port

    # ── 识别已有 config_client.json：可能在默认位置（上次跑过 install.sh）
    #    或用户自己 scp 到任意路径（推荐流程）。提取连接信息后跳过重复问询
    local default_cfg_path
    if [[ "$INSTALL_MODE" == "system" ]]; then
        default_cfg_path="${ETC_DIR}/config_client.json"
    else
        default_cfg_path="${WORK_DIR}/config_client.json"
    fi

    local cfg_path_input
    if [[ -f "$default_cfg_path" ]]; then
        info "默认位置已有配置：$default_cfg_path"
        cfg_path_input=$(ask "config_client.json 路径（回车用默认；输 '-' 跳过；或输绝对路径用其他）" "$default_cfg_path")
    else
        info "默认位置无现有配置：$default_cfg_path"
        cfg_path_input=$(ask "已有 config_client.json 想导入？输绝对路径（留空跳过、自行填全部字段）" "")
    fi

    local existing_cfg_path=""
    if [[ "$cfg_path_input" == "-" || -z "$cfg_path_input" ]]; then
        :   # 用户主动跳过
    elif [[ ! -f "$cfg_path_input" ]]; then
        warn "$cfg_path_input 不存在，跳过自动识别"
    elif _load_existing_client_cfg "$cfg_path_input"; then
        existing_cfg_path="$cfg_path_input"
    else
        warn "$cfg_path_input 解析失败或缺 mirage outbound，跳过自动识别"
    fi

    local existing_loaded=no
    if [[ -n "$existing_cfg_path" ]]; then
        info "导入：$existing_cfg_path"
        info "  服务端：${EXISTING_SERVER_HOST}:${EXISTING_SERVER_PORT}"
        info "  伪装 SNI：${EXISTING_CAMOUFLAGE}"
        info "  路由规则：${EXISTING_RULES_COUNT} 条"
        info "  DNS 转发器：${EXISTING_DNS_LISTEN:-未启用}"
        info "  Clash API：${EXISTING_API_LISTEN:-未启用}"
        if ask_yn "复用以上连接信息，只补齐分流 / DNS / API / 日志？" y; then
            server_host="$EXISTING_SERVER_HOST"
            server_port="$EXISTING_SERVER_PORT"
            password="$EXISTING_PASSWORD"
            camouflage="$EXISTING_CAMOUFLAGE"
            socks5_port="$EXISTING_SOCKS5_PORT"
            [[ -z "$socks5_port" ]] && socks5_port=$(ask_port "本地监听端口（mixed）" "7890")
            existing_loaded=yes
            ok "已复用连接信息（$server_host:$server_port），跳过这几项询问"
        fi
    fi

    local local_listen_host="0.0.0.0"     # 默认 LAN 共用；用户可改 127.0.0.1

    # 两端模式：直接复用刚装的服务端信息（连本机），只问本地监听端口
    if [[ "${BOTH_MODE:-0}" == "1" && "$existing_loaded" != "yes" ]]; then
        server_host="127.0.0.1"
        server_port="$INSTALLED_SERVER_PORT"
        password="$INSTALLED_SERVER_PASSWORD"
        camouflage="$INSTALLED_SERVER_CAMOUFLAGE"
        local_listen_host="127.0.0.1"
        ok "复用服务端信息：127.0.0.1:${server_port}（SNI ${camouflage}）"
        socks5_port=$(ask_port "本地监听端口（mixed = SOCKS5 + HTTP 同口）" "7890" tcp)
        existing_loaded=yes
    fi

    if [[ "$existing_loaded" != "yes" ]]; then
        NODES_HOST=(); NODES_PORT=(); NODES_PASSWORD=(); NODES_SNI=(); NODES_BRUTAL=()
        _collect_node
        while ask_yn "再添加一个节点？" n; do
            _collect_node
        done
        # 首节点同时填到旧变量，给末尾的提示文本复用
        server_host="${NODES_HOST[0]}"
        server_port="${NODES_PORT[0]}"
        password="${NODES_PASSWORD[0]}"
        camouflage="${NODES_SNI[0]}"
        local_listen_host=$(ask "本地监听地址（0.0.0.0 = LAN 设备共用；127.0.0.1 = 仅本机）" "0.0.0.0")
        socks5_port=$(ask_port "本地监听端口（mixed = SOCKS5 + HTTP 同口）" "7890" tcp)
    fi

    # 多节点 → 询问选路模式（单节点时跳过、走单 mirage outbound 旧路径）
    GROUP_TYPE=""
    if (( ${#NODES_HOST[@]} > 1 )); then
        echo
        info "已配置 ${#NODES_HOST[@]} 个节点。选路模式："
        local gchoice
        gchoice=$(ask_choice "选路模式" \
            "urltest — 自动选 BrutalPool 建连延迟最低的节点（带 50ms 防抖）" \
            "selector — 手动指定走哪个节点（启用 Clash API 后用 UI / curl 切换）")
        case "$gchoice" in
            1) GROUP_TYPE="urltest" ;;
            2) GROUP_TYPE="selector" ;;
        esac
        info "已选：${GROUP_TYPE}"
    fi

    # ── 路由模板 ──
    echo
    info "路由模板：决定哪些流量走代理"
    local choice routing_preset
    choice=$(ask_choice "选择" \
        "国内外分流（推荐）：geosite:cn / 内网 → 直连，其余走代理" \
        "全代理：所有流量走 proxy 出口" \
        "自定义高级：逐项配置 geo tag（广告/CN/GFW/流媒体等）" \
        "空规则：default=proxy + 空 rules，安装后自己编辑")
    case $choice in
        1) routing_preset="china_split" ;;
        2) routing_preset="transparent" ;;
        3) routing_preset="advanced"; ask_route_advanced ;;
        4) routing_preset="custom" ;;
    esac

    # ── DNS 方案 ──
    echo
    info "DNS 方案：决定 DNS 查询怎么走"
    info "  默认推荐：国内域名查 119.29.29.29 直连（快）；其余走 VPS 转发到 1.1.1.1（防污染）"
    local dns_preset
    if [[ "$routing_preset" == "transparent" ]]; then
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
    # DNS 详细配置（监听地址 / 端口 / 自定义上游）
    DNS_LISTEN_HOST="127.0.0.1"
    DNS_LISTEN_PORT="5353"
    DNS_CN="119.29.29.29"
    DNS_REMOTE="1.1.1.1:53"
    if [[ "$dns_preset" != "off" ]]; then
        ask_dns_details
    fi

    # ── TProxy 透明代理 ──
    echo
    info "TProxy 透明代理（可选；需要额外 iptables 配置，详见 README）"
    local tproxy_port=0
    if ask_yn "启用 TProxy？" n; then
        tproxy_port=$(ask_port "TProxy 监听端口（TCP）" "1081" tcp)
        warn "需手配 iptables TPROXY 规则。模板见 README『TProxy 防火墙规则』一节"
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

    ask_log_config

    # 复用既有配置（existing_loaded=yes）或 BOTH_MODE 路径里 NODES_* 是空的，
    # 从单节点变量回填，让 write_client_config 统一从 NODES_* 读取
    if (( ${#NODES_HOST[@]} == 0 )); then
        NODES_HOST=("$server_host")
        NODES_PORT=("$server_port")
        NODES_PASSWORD=("$password")
        NODES_SNI=("$camouflage")
        NODES_BRUTAL=(0)
    fi

    install_system_files
    install_shim_scripts "client"
    write_client_config "$local_listen_host" "$socks5_port" "$routing_preset" "$dns_preset" \
                        "$tproxy_port" "$enable_api" "$api_secret"

    install_service_unit "client"

    # 末尾提示
    title "客户端安装完成"
    cat <<EOF
$(_c 32 "✓ Mixed") 已监听 ${local_listen_host}:${socks5_port}（SOCKS5 + HTTP 同口）
EOF
    if [[ "$dns_preset" != "off" ]]; then
        echo "$(_c 32 "✓ DNS")    forwarder 监听 ${DNS_LISTEN_HOST}:${DNS_LISTEN_PORT}"
        echo "    将系统 DNS 改成 ${DNS_LISTEN_HOST}（或用 iptables 重定向 53→${DNS_LISTEN_PORT}）"
    fi
    if (( tproxy_port > 0 )); then
        echo "$(_c 32 "✓ TProxy") 监听 0.0.0.0:${tproxy_port}（需手配 iptables TPROXY 规则）"
    fi
    if [[ "$enable_api" == "yes" ]]; then
        echo "$(_c 32 "✓ Clash API") 监听 127.0.0.1:9090"
        echo "    secret: $api_secret"
    fi
    cat <<EOF

应用层用法（mixed 入口同时支持 SOCKS5 + HTTP）：
  - Chrome 等：设置 HTTP 或 SOCKS5 代理 127.0.0.1:${socks5_port} 都可
  - curl --socks5 127.0.0.1:${socks5_port} https://www.google.com
  - curl -x http://127.0.0.1:${socks5_port} https://www.google.com

常用命令（基于检测到的 init 系统 ${INIT_SYSTEM}）：
$(case "$INIT_SYSTEM" in
    systemd) echo "  systemctl status mirage-client          # 状态"
             echo "  systemctl reload mirage-client          # 热加载配置" ;;
    openrc)  echo "  rc-service mirage-client status         # 状态"
             echo "  rc-service mirage-client reload         # 热加载配置" ;;
    sysv)    echo "  service mirage-client status            # 状态"
             echo "  service mirage-client reload            # 热加载配置" ;;
    *)       echo "  kill -HUP \$(pgrep -f 'python3.*client.py')  # 热加载配置" ;;
esac)
  tail -f ${EFFECTIVE_LOG_DIR}/client.log    # 日志
EOF
    if [[ "$INSTALL_MODE" == "system" ]]; then
        cat <<EOF

CLI shim（在 PATH 里）：
  mirage-client                            # 用默认 cfg 启动
  mirage-client /path/to/other.json        # 指定 cfg
EOF
    fi
    cat <<EOF

进阶（手动编辑 ${EFFECTIVE_ETC}/config_client.json，改完热加载即可）：
  - 多节点 + urltest / fallback / selector 选路组
  - 多字段 AND 规则（"mode": "and"）
  详见 README.md「多节点与自适应选路」「分流规则」与 config_client.example.jsonc
EOF
}

# ──────────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# 卸载
# ──────────────────────────────────────────────────────────────────────────────

_uninstall_service_one() {
    local kind=$1
    # 两个 name：现有 mirage-* 和 legacy pyrealiy-*
    for name in "mirage-${kind}" "pyrealiy-${kind}"; do
        case "$INIT_SYSTEM" in
            systemd)
                if systemctl list-unit-files 2>/dev/null | grep -q "^${name}.service"; then
                    systemctl stop "${name}.service" 2>/dev/null || true
                    systemctl disable "${name}.service" >/dev/null 2>&1 || true
                    rm -f "/etc/systemd/system/${name}.service"
                    ok "removed systemd unit ${name}"
                fi
                ;;
            openrc)
                if [[ -f "/etc/init.d/${name}" ]]; then
                    rc-service "${name}" stop 2>/dev/null || true
                    rc-update del "${name}" default 2>/dev/null || true
                    rm -f "/etc/init.d/${name}"
                    ok "removed OpenRC service ${name}"
                fi
                ;;
            sysv)
                if [[ -f "/etc/init.d/${name}" ]]; then
                    service "${name}" stop 2>/dev/null || true
                    command -v update-rc.d &>/dev/null && update-rc.d -f "${name}" remove >/dev/null 2>&1 || true
                    command -v chkconfig &>/dev/null  && chkconfig --del "${name}" 2>/dev/null || true
                    rm -f "/etc/init.d/${name}"
                    ok "removed SysV init ${name}"
                fi
                ;;
        esac
    done
}

uninstall() {
    title "Mirage 卸载"

    info "停止 + 移除 service unit ..."
    _uninstall_service_one server
    _uninstall_service_one client
    [[ "$INIT_SYSTEM" == "systemd" ]] && systemctl daemon-reload 2>/dev/null

    # shim（含 legacy pyrealiy-* shim）
    local removed_shim=no
    for s in mirage-server mirage-client pyrealiy-server pyrealiy-client; do
        if [[ -f "$BIN_DIR/$s" ]]; then
            rm -f "$BIN_DIR/$s"
            removed_shim=yes
        fi
    done
    [[ "$removed_shim" == "yes" ]] && ok "removed shim scripts in $BIN_DIR"

    # logrotate 配置
    local removed_lr=no
    for f in /etc/logrotate.d/mirage-server /etc/logrotate.d/mirage-client \
             /etc/logrotate.d/pyrealiy-server /etc/logrotate.d/pyrealiy-client; do
        if [[ -f "$f" ]]; then
            rm -f "$f"
            removed_lr=yes
        fi
    done
    [[ "$removed_lr" == "yes" ]] && ok "removed logrotate configs"

    # program tree（现有 + legacy）
    for dir in "$INSTALL_PREFIX" "$LEGACY_INSTALL_PREFIX"; do
        if [[ -d "$dir" ]]; then
            if ask_yn "删除程序目录 $dir？" y; then
                rm -rf "$dir"
                ok "removed $dir"
            else
                warn "保留 $dir"
            fi
        fi
    done

    # config（默认保留，含密码！）
    for dir in "$ETC_DIR" "$LEGACY_ETC_DIR"; do
        if [[ -d "$dir" ]]; then
            if ask_yn "删除配置目录 $dir（含密码！请确认已备份）？" n; then
                rm -rf "$dir"
                ok "removed $dir"
            else
                warn "保留 $dir"
            fi
        fi
    done

    # logs
    for dir in "$LOG_DIR" "$LEGACY_LOG_DIR"; do
        if [[ -d "$dir" ]]; then
            if ask_yn "删除日志目录 $dir？" n; then
                rm -rf "$dir"
                ok "removed $dir"
            else
                warn "保留 $dir"
            fi
        fi
    done

    # state（geosite 缓存）
    for dir in "$STATE_DIR" "$LEGACY_STATE_DIR"; do
        if [[ -d "$dir" ]]; then
            if ask_yn "删除状态目录 $dir（含 geosite 缓存）？" n; then
                rm -rf "$dir"
                ok "removed $dir"
            else
                warn "保留 $dir"
            fi
        fi
    done

    title "卸载完成"
    info "如果还想完全清掉，手动检查 / 删："
    info "  /etc/systemd/system/mirage-*.service"
    info "  /etc/init.d/mirage-*"
    info "  ${BIN_DIR}/mirage-*"
    info "Python pip 依赖（cryptography / uvloop）未卸载（其他工具可能在用）"
}

# ──────────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────────

main() {
    title "Mirage v${MIRAGE_VERSION} 安装向导"

    check_linux
    check_root
    detect_pkg_mgr
    detect_init_system

    if [[ "${1:-}" =~ ^(info|show|export|config)$ ]]; then
        check_python
        export_server_info
        return
    fi

    local choice mode
    choice=$(ask_choice "操作类型" \
        "服务端安装" \
        "客户端安装" \
        "两端都装（本地测试用）" \
        "查看与导出现有服务端连接信息" \
        "卸载（停服务、删文件）")
    case $choice in
        1) mode="server" ;;
        2) mode="client" ;;
        3) mode="both" ;;
        4) mode="info" ;;
        5) mode="uninstall" ;;
    esac

    if [[ "$mode" == "uninstall" ]]; then
        uninstall
        return
    elif [[ "$mode" == "info" ]]; then
        check_python
        export_server_info
        return
    fi

    # 安装路径要求源文件 + python + openssl
    if ! [[ -f "${WORK_DIR}/server.py" && -f "${WORK_DIR}/client.py" ]]; then
        err "源文件不在 $WORK_DIR（看不到 server.py / client.py）。请在仓库目录下运行。"
    fi
    check_python
    check_openssl

    choose_install_mode
    resolve_paths

    install_pip_deps

    case $mode in
        server) install_server ;;
        client) install_client ;;
        both)
            BOTH_MODE=1                      # 让 install_server 跳过"拷到远程客户端"模板
            install_server
            echo
            info "服务端装完，现在装客户端——自动复用刚才的连接信息（连本机 127.0.0.1）"
            ask_yn "继续装客户端吗？" y && install_client
            ;;
    esac

    title "全部完成"
    ok "感谢使用 Mirage v${MIRAGE_VERSION}"
    cat <<EOF

    安装模式：${INSTALL_MODE}
    程序  : ${EFFECTIVE_LIB}
    配置  : ${EFFECTIVE_ETC}
    日志  : ${EFFECTIVE_LOG_DIR}
    状态  : ${EFFECTIVE_GEOSITE_DIR}

    高级配置请参考带注释的示例文件：
      ${EFFECTIVE_LIB}/config_server.example.jsonc
      ${EFFECTIVE_LIB}/config_client.example.jsonc

    schema 合约 / Clash API / 热加载 / DoH-DoT 详见 README.md

    要卸载：重新运行 install.sh 选 4
EOF
}

main "$@"
