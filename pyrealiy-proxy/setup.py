#!/usr/bin/env python3
"""
PyReality 交互式部署脚本

自动检测系统环境、安装 TCP Brutal 内核模块、生成配置文件。
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import sys

# ── 终端颜色 ──────────────────────────────────────────────────────────────────
def _c(code, text): return f"\033[{code}m{text}\033[0m"
INFO  = lambda t: print(_c("36", f"[*] {t}"))
OK    = lambda t: print(_c("32", f"[✓] {t}"))
WARN  = lambda t: print(_c("33", f"[!] {t}"))
ERR   = lambda t: print(_c("31", f"[✗] {t}"))
TITLE = lambda t: print(_c("1;35", f"\n{'═'*50}\n  {t}\n{'═'*50}"))

def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"    {prompt}{hint}: ").strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)

def ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({hint})", "y" if default else "n").lower()
    return val in ("y", "yes", "")


# ── 输入校验 ──────────────────────────────────────────────────────────────────
# 历史教训：一次 server_host 写成 "172.245145.188"（缺一个点），客户端
# 把它当域名扔进 getaddrinfo → gaierror → SYN 一个都没发出去，排查耗了大半天。
# 所有 IP / 端口 / 域名输入都强制循环校验，输错立即重输，让低级错误进不了配置。

# RFC 1035 简化：每段 1-63 字符、字母数字或中划线、不能首尾连字符
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*$"
)


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _is_valid_hostname(s: str) -> bool:
    """允许单标签（如 'localhost' / 'router'），匹配 RFC 1035 一般主机名"""
    return bool(_DOMAIN_RE.match(s))


def _is_valid_fqdn(s: str) -> bool:
    """必须含至少一个点 —— 用于 camouflage_host，SNI 用单标签没意义"""
    return _is_valid_hostname(s) and "." in s


def ask_port(prompt: str, default: str = "") -> int:
    """端口（1-65535）。返回 int。输错立即重输。"""
    while True:
        v = ask(prompt, default).strip()
        try:
            p = int(v)
            if 1 <= p <= 65535:
                return p
            ERR(f"端口必须在 1-65535 范围，{p} 越界")
        except ValueError:
            ERR(f"端口必须是整数，{v!r} 不是")


def ask_ip(prompt: str, default: str = "") -> str:
    """必须是合法 IP 字面量。用于 DNS 服务器、监听地址等不接受域名的字段。"""
    while True:
        v = ask(prompt, default).strip()
        if _looks_like_ip(v):
            return v
        ERR(f"必须是合法 IP（v4 或 v6），{v!r} 不是 — 看是否漏点 / 多空格 / 含非法字符")


def ask_ip_or_domain(prompt: str, default: str = "",
                     verify_dns: bool = True) -> str:
    """
    IP 字面量或合法主机名（允许单标签，如 localhost）。
    这是踩过坑的字段（server_host），所以默认要试一次 DNS 解析，
    解析不到给警告并让用户确认是否照填。

    AF_UNSPEC 同时拿 A 和 AAAA，IPv6-only 也能用。
    """
    while True:
        v = ask(prompt, default).strip()
        if not v:
            ERR("不能为空")
            continue
        if _looks_like_ip(v):
            return v
        if not _is_valid_hostname(v):
            ERR(f"既不是合法 IP 也不是合法主机名：{v!r}（典型问题：多空格 / 含 http:// / 含端口）")
            continue
        if verify_dns:
            try:
                socket.getaddrinfo(v, None, family=socket.AF_UNSPEC)
            except socket.gaierror as e:
                WARN(f"DNS 解析 {v} 失败：{e}")
                if not ask_yn(f"仍然使用 {v}（可能是临时网络问题或域名暂未生效）", default=False):
                    continue
        return v


def ask_domain(prompt: str, default: str = "") -> str:
    """
    必须是 FQDN（含至少一个点）。用于 camouflage_host：
      - IP 不能作为 SNI
      - 单标签（如 localhost）作为 SNI 没意义，无法伪装成真实公网站点
    """
    while True:
        v = ask(prompt, default).strip()
        if _looks_like_ip(v):
            ERR(f"camouflage 必须是域名而非 IP，{v} 是 IP（SNI 不接受 IP 字面量）")
            continue
        if not _is_valid_fqdn(v):
            ERR(f"不是合法 FQDN：{v!r}（必须至少含一个点，单标签如 'localhost' 不能作 SNI）")
            continue
        return v


def _probe_camouflage(host: str, port: int = 443, timeout: float = 10.0) -> tuple[bool, str]:
    """
    对 camouflage 目标做一次真实 TLS 1.3 握手测试 + 证书链验证。

    pyrealiy 服务端启动时会从这个站点缓存 TLS 1.3 握手记录用于回放伪装，
    所以必须满足：
      - DNS 解析得通
      - TCP 443 可达
      - 协商 TLS **1.3**（我们的缓存格式不支持 1.2）
      - 证书链有效（避免被 GFW 中间人塞假证）

    返回 (ok, message)，message 是给用户看的中文反馈。
    """
    import ssl as _ssl
    import time as _time

    ctx = _ssl.create_default_context()
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_3   # 不接受 1.2 协商下来

    start = _time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                version = ssock.version()
                elapsed = (_time.monotonic() - start) * 1000
                if version == "TLSv1.3":
                    return True, f"TLS 1.3 握手 OK（{elapsed:.0f} ms）"
                return False, f"握手成功但协商版本是 {version}，需要 TLS 1.3"
    except socket.gaierror as e:
        return False, f"DNS 解析失败：{e}"
    except socket.timeout:
        return False, f"连接超时（{timeout:.0f}s 内无响应）"
    except _ssl.SSLCertVerificationError as e:
        return False, f"证书验证失败：{e.verify_message}（可能被中间人或证书过期）"
    except _ssl.SSLError as e:
        return False, f"TLS 握手失败：{e}（站点可能不支持 TLS 1.3）"
    except ConnectionRefusedError:
        return False, f"端口 {port} 拒绝连接（站点未在该端口提供 TLS）"
    except OSError as e:
        return False, f"网络错误：{e}"


def ask_camouflage_host(prompt: str, default: str = "") -> str:
    """
    询问 camouflage 域名 + 自动做一次 TLS 1.3 握手测试。

    失败时让用户选：
      [1] 改填别的域名（推荐）
      [2] 仍使用此域名（启动时可能失败，或本地网络问题导致探测失误）
    """
    while True:
        v = ask_domain(prompt, default)
        INFO(f"正在测试 {v}:443 是否可作为 camouflage 目标 ...")
        ok, msg = _probe_camouflage(v)
        if ok:
            OK(msg)
            return v
        ERR(msg)
        choice = ask(
            "如何处理？[1] 改填别的域名（推荐） [2] 仍使用此域名",
            "1",
        ).strip()
        if choice == "2":
            WARN(f"强制使用 {v}，pyrealiy server 启动时若 'Handshake cache warmup failed' "
                 f"则探测流量得不到伪装，仅密钥认证仍有效")
            return v
        # 其它输入（含 "1"）→ 循环重输


# ── 系统检测 ──────────────────────────────────────────────────────────────────

def check_linux() -> bool:
    if platform.system() != "Linux":
        WARN(f"当前系统为 {platform.system()}，TCP Brutal 仅支持 Linux")
        return False
    return True

def check_root() -> bool:
    return os.geteuid() == 0

TCP_CONGESTION = 13  # 与 core/brutal.py 保持一致

def brutal_is_loaded() -> bool:
    """测试 tcp_brutal 内核模块是否已加载"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, "brutal".encode())
        return True
    except OSError:
        return False

def get_default_interface() -> str:
    """获取默认路由使用的网络接口名"""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True
        )
        m = re.search(r"dev (\S+)", out)
        return m.group(1) if m else "eth0"
    except Exception:
        return "eth0"

def get_interface_speed_mbps(iface: str) -> int:
    """从 sysfs 读取网卡速率（Mbps），失败返回 0"""
    try:
        path = f"/sys/class/net/{iface}/speed"
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return 0

def suggest_brutal_rate(iface: str) -> int:
    """建议的单连接 Brutal 速率（字节/秒）"""
    speed = get_interface_speed_mbps(iface)
    if speed <= 0:
        return 8_000_000   # 默认 8 Mbps
    # 按照总带宽的 8%~15% 设单连接速率，多连接叠加使用
    per_conn = max(5, min(20, speed // 10))
    return per_conn * 1_000_000


# ── TCP Brutal 安装 ───────────────────────────────────────────────────────────

def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)

def install_brutal_official() -> bool:
    """使用官方一键脚本安装 tcp_brutal（推荐，自动处理 DKMS 和开机加载）"""
    INFO("使用官方一键脚本安装 tcp_brutal ...")
    r = subprocess.run(
        "bash <(curl -fsSL https://tcp.hy2.sh/)",
        shell=True, executable="/bin/bash",
    )
    if r.returncode != 0:
        ERR("官方脚本安装失败，请查看上方输出")
        return False
    # 脚本通常已自动加载模块，此处补一次 modprobe 确保立即生效
    subprocess.run(["modprobe", "tcp_brutal"], capture_output=True)
    return True


def install_brutal_dkms() -> bool:
    """通过 DKMS 安装 tcp_brutal（持久化，重启后仍有效）"""
    INFO("尝试通过 DKMS 安装 tcp_brutal ...")
    # 含 $(...) shell 替换的步骤必须用字符串 + shell=True 才能展开；
    # 之前传 list + shell=True 会导致 sh 只把第一个元素当命令、其余作为 $0..$N，
    # 实际执行的是无参 apt-get，全部步骤静默失败
    steps = [
        ("apt-get update -q",                                                 "更新软件源"),
        ('apt-get install -y dkms "linux-headers-$(uname -r)"',               "安装 DKMS 和内核头文件"),
        ("git clone --depth=1 https://github.com/apernet/tcp-brutal /tmp/tcp-brutal",
                                                                              "克隆源码"),
        ("cd /tmp/tcp-brutal && make dkms",                                   "DKMS 安装模块"),
        ("modprobe tcp_brutal",                                               "加载模块"),
    ]
    for cmd, desc in steps:
        INFO(desc)
        r = subprocess.run(cmd, shell=True, executable="/bin/bash")
        if r.returncode != 0:
            ERR(f"步骤失败：{desc}")
            return False
    return True

def install_brutal_simple() -> bool:
    """编译并 insmod（临时，重启后失效）"""
    INFO("编译并临时加载 tcp_brutal ...")
    cmds = [
        (["apt-get", "install", "-y", "-q", "build-essential", "linux-headers-$(uname -r)"],
         True),
        (["git", "clone", "--depth=1",
          "https://github.com/apernet/tcp-brutal", "/tmp/tcp-brutal"],
         False),
        (["bash", "-c", "cd /tmp/tcp-brutal && make && insmod tcp_brutal.ko"],
         True),
    ]
    for cmd, use_shell in cmds:
        r = _run(" ".join(cmd) if use_shell else cmd, shell=use_shell)
        if r.returncode != 0:
            ERR("编译安装失败，请查看上方输出")
            return False
    # 写入 /etc/modules 实现开机自动加载
    try:
        ko_path = subprocess.check_output(
            ["find", "/tmp/tcp-brutal", "-name", "tcp_brutal.ko"], text=True
        ).strip()
        if ko_path:
            dest = "/lib/modules/tcp_brutal.ko"
            _run(["cp", ko_path, dest])
            with open("/etc/modules", "a") as f:
                f.write("\ntcp_brutal\n")
            OK("已写入 /etc/modules，下次启动自动加载")
    except Exception:
        WARN("无法设置开机自动加载，请手动执行：insmod tcp_brutal.ko")
    return True

def handle_brutal_install() -> bool:
    """引导用户安装 TCP Brutal，返回安装后是否可用"""
    if brutal_is_loaded():
        OK("TCP Brutal 内核模块已就绪")
        return True

    if not check_linux():
        return False

    WARN("未检测到 tcp_brutal 内核模块")
    if not ask_yn("是否现在安装 TCP Brutal 内核模块？"):
        INFO("跳过安装，将使用普通 TCP（brutal_rate_bps 将设为 0）")
        return False

    if not check_root():
        ERR("安装内核模块需要 root 权限，请用 sudo 运行此脚本")
        return False

    method = ask(
        "安装方式：[1] 官方一键脚本（推荐）  [2] DKMS 手动编译  [3] 临时加载",
        default="1",
    )
    if method == "3":
        ok = install_brutal_simple()
    elif method == "2":
        ok = install_brutal_dkms()
    else:
        ok = install_brutal_official()

    if ok and brutal_is_loaded():
        OK("TCP Brutal 安装成功")
        return True
    else:
        ERR("安装失败，将使用普通 TCP")
        return False


# ── 配置生成 ──────────────────────────────────────────────────────────────────

def configure_server(brutal_available: bool) -> None:
    TITLE("服务端配置")

    listen_port   = ask_port("监听端口", "443")
    password      = ask("连接密码（建议用随机字符串）")
    camouflage    = ask_camouflage_host("伪装域名", "www.apple.com")

    rate_bps = 0
    if brutal_available:
        iface = get_default_interface()
        speed = get_interface_speed_mbps(iface)
        suggested = suggest_brutal_rate(iface)
        INFO(f"检测到网卡 {iface}，带宽 {speed} Mbps，TCP Brutal 已启用")
        rate_input = ask(
            "单连接 Brutal 速率（Mbps）",
            str(suggested // 1_000_000),
        )
        rate_bps = int(rate_input) * 1_000_000
    else:
        INFO("TCP Brutal 不可用，使用普通 TCP")

    # ── Web 管理面板 ──────────────────────────────────────────────────────────
    admin_cfg: dict = {"admin_port": 0}
    if ask_yn("是否启用 Web 管理面板（监控连接、封锁 IP）？", default=False):
        admin_host  = ask_ip("面板监听地址（建议 127.0.0.1 仅本机，0.0.0.0 公网暴露需设令牌）",
                             "127.0.0.1")
        admin_port  = ask_port("面板端口", "8080")
        admin_token = ask("访问令牌（留空则不验证，建议随机字符串）", "")
        admin_cfg   = {
            "admin_host":  admin_host,
            "admin_port":  admin_port,
            "admin_token": admin_token,
        }
        token_hint = f"?token={admin_token}" if admin_token else ""
        OK(f"面板地址：http://{admin_host}:{admin_port}/{token_hint}")
        if admin_host == "127.0.0.1":
            INFO("面板仅本机可访问，可通过 SSH 隧道在本地浏览器查看：")
            print(f"    {_c('36', f'ssh -L {admin_port}:127.0.0.1:{admin_port} user@your-vps')}")

    cfg = {
        "listen_host":    "0.0.0.0",
        "listen_port":    listen_port,
        "password":       password,
        "camouflage_host": camouflage,
        "camouflage_port": 443,
        "brutal_rate_bps": rate_bps,
        **admin_cfg,
    }

    # ── WireGuard egress（典型用例：Cloudflare WARP）────────────────────────────
    configure_warp_egress(cfg)

    path = "config_server.json"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
    OK(f"服务端配置已写入 {path}")


# ── WireGuard / Cloudflare WARP 集成 ──────────────────────────────────────────

_WARP_DEFAULT_TAGS = [
    ("Netflix",             "netflix"),
    ("Disney+",             "disney"),
    ("OpenAI / ChatGPT",    "openai"),
    ("Spotify",             "spotify"),
    ("YouTube",             "youtube"),
    ("Google 全球",         "google"),
]


def configure_warp_egress(cfg: dict) -> None:
    """询问是否启用 WARP egress；启用则写 egress 配置 + 生成 setup-warp.sh"""
    print()
    if not ask_yn(
        "是否启用 Cloudflare WARP 出口（让 Netflix/OpenAI 等被 VPS IP 封的服务走 CF 出口）？",
        default=False,
    ):
        return

    if not check_linux():
        WARN("非 Linux 系统暂不支持，跳过 WARP 配置")
        return

    mark_hex = ask("WARP 流量 fwmark（十六进制，默认 0x100）", "0x100").strip()
    try:
        mark = int(mark_hex, 16) if mark_hex.startswith(("0x", "0X")) else int(mark_hex)
        if not 1 <= mark <= 0xFFFFFFFF:
            raise ValueError
    except ValueError:
        ERR(f"fwmark {mark_hex!r} 不合法，使用默认 0x100")
        mark = 0x100

    table = ask("WARP 路由表号（默认 200）", "200").strip()
    try:
        table = int(table)
    except ValueError:
        table = 200

    # ── 选择走 WARP 的应用 ─────────────────────────────────────────────────────
    print()
    INFO("选择哪些服务走 WARP（其余走 VPS 默认出口）：")
    defaults: list[int] = []
    for i, (name, tag) in enumerate(_WARP_DEFAULT_TAGS, start=1):
        print(f"    [{i:>2}] {name:<22} GEOSITE,loyalsoldier:{tag}")
        defaults.append(i)
    raw = ask("启用的编号（逗号分隔，回车 = 全部）", ",".join(map(str, defaults)))
    sel = _parse_selection(raw, len(_WARP_DEFAULT_TAGS))
    if not sel:
        WARN("一个都没选，但仍写入 egress 定义（你可以手工编辑 egress_rules）")

    egress_rules: list[str] = []
    for idx in sel:
        _, tag = _WARP_DEFAULT_TAGS[idx - 1]
        egress_rules.append(f"GEOSITE,loyalsoldier:{tag},WARP")
    egress_rules.append("FINAL,DIRECT")

    # ── 写入 cfg ───────────────────────────────────────────────────────────────
    cfg["egresses"] = [{"name": "WARP", "mark": mark}]
    cfg["egress_rules"] = egress_rules
    needs_site = any("GEOSITE" in r for r in egress_rules)
    needs_ip   = any("GEOIP"   in r for r in egress_rules)
    cfg["geosite_dir"]         = ".geosite"
    cfg["geosite_update_days"] = 7
    cfg["geosite_sources"]     = [{"name": "loyalsoldier", "url": _LOYALSOLDIER_SITE_URLS}] if needs_site else []
    cfg["geoip_sources"]       = [{"name": "loyalsoldier", "url": _LOYALSOLDIER_IP_URLS}] if needs_ip else []

    # ── 生成一键脚本 ───────────────────────────────────────────────────────────
    sh = _gen_warp_setup_script(mark, table)
    with open("setup-warp.sh", "w") as f:
        f.write(sh)
    os.chmod("setup-warp.sh", 0o755)

    OK("WARP egress 已写入 config_server.json")
    OK("已生成 setup-warp.sh（用 wgcf 自动注册免费 WARP 账号 + 配置 wg-quick + 策略路由）")
    INFO("下一步：")
    print(f"    {_c('36', 'sudo bash setup-warp.sh')}")
    INFO("脚本会做：1) 装 wireguard + wgcf  2) 注册 WARP  3) 写 /etc/wireguard/warp.conf")
    print(f"    {_c('36', '    4) 启动 wg-quick@warp 并加入开机自启  5) 配 ip rule / ip route')}")


def _gen_warp_setup_script(mark: int, table: int) -> str:
    """生成 setup-warp.sh —— Debian/Ubuntu 上一键拉起 WARP egress"""
    return f"""#!/bin/bash
# PyReality WARP egress 一键脚本（由 setup.py 生成）
# 作用：在 VPS 上拉起一条 Cloudflare WARP WireGuard 隧道，
#       让 PyReality server 通过 SO_MARK={mark:#x} 把命中规则的流量送进去。
#
# 适用：Debian/Ubuntu。其它发行版需要手工调整包名。
# 需要 root。

set -e

FWMARK={mark}
TABLE={table}
WG_IFACE=warp
WG_CONF=/etc/wireguard/$WG_IFACE.conf

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root / sudo 运行"
    exit 1
fi

echo "[1/5] 安装 wireguard-tools + iproute2 + curl ..."
apt-get update -q
apt-get install -y -q wireguard-tools iproute2 curl

echo "[2/5] 装 wgcf（注册 WARP 拿 WG 配置的工具）..."
WGCF_VERSION=$(curl -s https://api.github.com/repos/ViRb3/wgcf/releases/latest \\
    | grep -oP '"tag_name":\\s*"\\K[^"]+' || echo "v2.2.22")
if [ -z "$WGCF_VERSION" ]; then WGCF_VERSION=v2.2.22; fi
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  WGCF_ARCH=amd64 ;;
    aarch64) WGCF_ARCH=arm64 ;;
    armv7l)  WGCF_ARCH=arm   ;;
    *) echo "未识别架构 $ARCH"; exit 1 ;;
esac
curl -L -o /usr/local/bin/wgcf \\
    "https://github.com/ViRb3/wgcf/releases/download/$WGCF_VERSION/wgcf_${{WGCF_VERSION#v}}_linux_$WGCF_ARCH"
chmod +x /usr/local/bin/wgcf

echo "[3/5] 注册 WARP 免费账户（生成 wgcf-account.toml + wgcf-profile.conf）..."
WGCF_WORKDIR=/root/.warp
mkdir -p "$WGCF_WORKDIR"
cd "$WGCF_WORKDIR"
if [ ! -f wgcf-account.toml ]; then
    wgcf register --accept-tos
fi
wgcf generate

echo "[4/5] 写 $WG_CONF 并启 wg-quick@$WG_IFACE ..."
# 在生成的 wgcf-profile.conf 基础上加 Table=off（禁掉它默认抢全局路由），
# 我们用自己的策略路由表 $TABLE 来定向流量
awk '
    /^\\[Interface\\]/ {{ print; print "Table = off"; next }}
    {{ print }}
' wgcf-profile.conf > "$WG_CONF"
chmod 600 "$WG_CONF"

systemctl enable --now wg-quick@$WG_IFACE
sleep 1

echo "[5/5] 配 ip rule / ip route（让 fwmark $FWMARK 的包走 WARP）..."
# 持久化：写到 /etc/networkd-dispatcher 或在 PostUp 里都行；这里走 wg-quick 的 PostUp
if ! grep -q "PostUp = ip rule add" "$WG_CONF"; then
    sed -i "/^\\[Interface\\]/a\\
PostUp = ip rule add fwmark $FWMARK lookup $TABLE; ip route add default dev $WG_IFACE table $TABLE\\
PostDown = ip rule del fwmark $FWMARK lookup $TABLE; ip route flush table $TABLE" "$WG_CONF"
    systemctl restart wg-quick@$WG_IFACE
fi

# 立即生效（如果还没有）
ip rule add fwmark $FWMARK lookup $TABLE 2>/dev/null || true
ip route add default dev $WG_IFACE table $TABLE 2>/dev/null || true

echo
echo "[✓] WARP egress 已就绪"
echo "    接口  : $WG_IFACE"
echo "    fwmark: $FWMARK ($(printf '%#x' $FWMARK))"
echo "    table : $TABLE"
echo
echo "测试（应看到 Cloudflare WARP 出口 IP）:"
echo "    curl --interface $WG_IFACE -s https://cloudflare.com/cdn-cgi/trace | grep -E 'ip|warp'"
echo "    或：sudo -E ip vrf exec $WG_IFACE curl -s https://ifconfig.me  （v4 only）"
echo
echo "PyReality server 启动后，命中 egress_rules 的目标自动走这条隧道。"
"""


# ── 分流规则配置 ──────────────────────────────────────────────────────────────

# (展示名, tag, 动作, 默认启用, 说明)
_GEOSITE_CATALOG = [
    # ── REJECT ──────────────────────────────────────────────────────────
    ("广告与追踪屏蔽",         "category-ads-all",    "REJECT", True,
     "广告、追踪脚本、恶意域名"),

    # ── DIRECT ──────────────────────────────────────────────────────────
    ("私有 / 本地域名",        "private",             "DIRECT", True,
     "localhost、内网等私有域名"),
    ("中国大陆域名",           "cn",                  "DIRECT", True,
     "百度、淘宝、B站等大陆综合域名列表"),
    ("苹果中国服务",           "apple-cn",            "DIRECT", True,
     "苹果在中国大陆的 CDN 和下载节点"),
    ("谷歌中国服务",           "google-cn",           "DIRECT", True,
     "谷歌在中国大陆可访问的服务（地图、商店等）"),

    # ── PROXY ───────────────────────────────────────────────────────────
    ("GFW 封锁域名",           "gfw",                 "PROXY",  True,
     "已知被防火长城封锁的域名（精确维护列表）"),
    ("境外常见域名",           "geolocation-!cn",     "PROXY",  False,
     "非中国大陆的常用域名（大而全，可替代 gfw 规则）"),
    ("YouTube",               "youtube",             "PROXY",  False, ""),
    ("Google（全球）",         "google",              "PROXY",  False,
     "谷歌全球服务（含 YouTube 以外的其他产品）"),
    ("Telegram",              "telegram",            "PROXY",  False, ""),
    ("Twitter / X",           "twitter",             "PROXY",  False, ""),
    ("Facebook / Instagram",  "facebook",            "PROXY",  False,
     "Facebook、Instagram、WhatsApp、Threads"),
    ("OpenAI / ChatGPT",      "openai",              "PROXY",  False, ""),
    ("GitHub",                "github",              "PROXY",  False, ""),
    ("Netflix",               "netflix",             "PROXY",  False, ""),
    ("Disney+",               "disney",              "PROXY",  False, ""),
    ("Pixiv",                 "pixiv",               "PROXY",  False, ""),
    ("学术站点",              "category-scholar-!cn", "PROXY",  False,
     "谷歌学术、arXiv、Sci-Hub 等境外学术资源"),
]

_GEOIP_CATALOG = [
    ("中国大陆 IP 直连", "cn",      "DIRECT", True,
     "IP 归属地为中国大陆，直连（补全域名规则漏网的直连 IP 流量）"),
    ("私有 IP 直连",    "private",  "DIRECT", True,
     "RFC1918 私有地址段（10.x、172.16.x、192.168.x）"),
]

# 多镜像 fallback：首选官方源（隧道走得通时最快最新），失败按序回落到 CN 友好的 CDN
# 注：第三方代理镜像（gh-proxy / ghfast 等）会随时间变化，必要时手动替换
_LOYALSOLDIER_SITE_URLS = [
    "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
    "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat",
    "https://gh-proxy.com/https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
]
_LOYALSOLDIER_IP_URLS = [
    "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat",
    "https://gh-proxy.com/https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
]

_ACTION_COLOR = {"REJECT": "31", "DIRECT": "32", "PROXY": "33"}
_ACTION_LABEL = {"REJECT": "屏蔽", "DIRECT": "直连", "PROXY": "代理"}


def _print_catalog(catalog, title: str) -> list[int]:
    """打印规则列表，返回默认启用的编号列表（1-based）"""
    print()
    print(_c("1;36", f"  {title}"))
    print()

    last_action = None
    defaults: list[int] = []

    for idx, (name, tag, action, default, desc) in enumerate(catalog, start=1):
        # 动作分组标题
        if action != last_action:
            label = _ACTION_LABEL[action]
            color = _ACTION_COLOR[action]
            print(f"    {_c(color, f'── {label} ({action}) ' + '─' * 20)}")
            last_action = action

        star  = _c("33", "✦") if default else " "
        num   = _c("1", f"[{idx:>2}]")
        nstr  = f"{name:<22}"
        tstr  = _c("36", f"{tag}")
        dstr  = f"  {_c('90', desc)}" if desc else ""
        print(f"    {star} {num} {nstr} {tstr}{dstr}")

        if default:
            defaults.append(idx)

    return defaults


def _parse_selection(raw: str, max_idx: int) -> list[int]:
    """解析用户输入的编号字符串，返回有效编号列表"""
    result: list[int] = []
    for tok in raw.replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= max_idx:
                result.append(n)
    return result


def configure_rules() -> dict:
    """
    交互式规则配置。
    返回包含 geosite_sources / geoip_sources / rules 的 dict。
    """
    TITLE("分流规则配置")

    INFO("规则数据来自 Loyalsoldier v2ray-rules-dat（启动时自动下载，每 7 天刷新）")
    print()

    if not ask_yn("是否配置分流规则（不配置则所有流量走代理）？"):
        return {
            "geosite_dir": ".geosite",
            "geosite_update_days": 7,
            "geosite_sources": [],
            "geoip_sources": [],
            "rules": ["FINAL,PROXY"],
        }

    # ── GEOSITE 选择 ─────────────────────────────────────────────────────────
    site_defaults = _print_catalog(_GEOSITE_CATALOG, "GEOSITE 域名规则")
    default_str   = ",".join(map(str, site_defaults))
    print()
    raw_site = ask(
        f"选择启用的规则编号（逗号分隔，✦ 为推荐项，直接回车使用推荐）",
        default_str,
    )
    site_sel = _parse_selection(raw_site, len(_GEOSITE_CATALOG))

    # ── GEOIP 选择 ───────────────────────────────────────────────────────────
    ip_defaults = _print_catalog(_GEOIP_CATALOG, "GEOIP IP 归属地规则")
    default_str = ",".join(map(str, ip_defaults))
    print()
    raw_ip = ask(
        "选择启用的规则编号",
        default_str,
    )
    ip_sel = _parse_selection(raw_ip, len(_GEOIP_CATALOG))

    # ── 默认动作 ─────────────────────────────────────────────────────────────
    print()
    final_action = ask(
        "未命中规则的默认动作 [1] PROXY（代理，推荐）  [2] DIRECT（直连）",
        "1",
    )
    final = "DIRECT" if final_action.strip() == "2" else "PROXY"

    # ── 组装规则 ─────────────────────────────────────────────────────────────
    # router 已改为 Clash 兼容的"配置顺序首中即返回"语义，
    # 因此即使用户输入选号是乱序（"12,1,4"），生成的规则也必须按 catalog 顺序排列。
    # _GEOSITE_CATALOG / _GEOIP_CATALOG 已按 REJECT → DIRECT → PROXY 分块排好，
    # 所以按 idx 排序后输出即可得到正确的优先级：屏蔽 > 直连 > 代理 > 默认。
    site_sel = sorted(set(site_sel))
    ip_sel   = sorted(set(ip_sel))

    rules: list[str] = []

    for idx in site_sel:
        _, tag, action, _, _ = _GEOSITE_CATALOG[idx - 1]
        rules.append(f"GEOSITE,loyalsoldier:{tag},{action}")

    for idx in ip_sel:
        _, code, action, _, _ = _GEOIP_CATALOG[idx - 1]
        rules.append(f"GEOIP,loyalsoldier:{code},{action}")

    # 静态局域网 CIDR（始终加入，放最末以兜住未被上面 GEOIP private 覆盖的情况）
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        rules.append(f"IP-CIDR,{cidr},DIRECT")

    rules.append(f"FINAL,{final}")

    # ── 汇总展示 ─────────────────────────────────────────────────────────────
    print()
    INFO(f"已生成 {len(rules)} 条规则，默认动作：{final}")

    needs_site = any("GEOSITE" in r for r in rules)
    needs_ip   = any("GEOIP"   in r for r in rules)

    return {
        "geosite_dir":         ".geosite",
        "geosite_update_days": 7,
        "geosite_sources": [
            {"name": "loyalsoldier", "url": _LOYALSOLDIER_SITE_URLS}
        ] if needs_site else [],
        "geoip_sources": [
            {"name": "loyalsoldier", "url": _LOYALSOLDIER_IP_URLS}
        ] if needs_ip else [],
        "rules": rules,
    }


# ── TProxy 透明代理配置 ───────────────────────────────────────────────────────

_TPROXY_MARK  = "0x1"
_TPROXY_TABLE = "100"
_PRIVATES     = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

_ROUTE_CMDS = [
    "# 路由：带标记的包走 loopback，使其再次经过 PREROUTING",
    f"ip rule add fwmark {_TPROXY_MARK} table {_TPROXY_TABLE}",
    f"ip route add local 0.0.0.0/0 dev lo table {_TPROXY_TABLE}",
]
_ROUTE_CLEAN = [
    f"ip rule del fwmark {_TPROXY_MARK} table {_TPROXY_TABLE} 2>/dev/null || true",
    f"ip route del local 0.0.0.0/0 dev lo table {_TPROXY_TABLE} 2>/dev/null || true",
]


def _detect_firewall() -> str:
    """检测系统可用的防火墙工具，优先 nftables"""
    for tool, name in [("nft", "nftables"), ("iptables", "iptables")]:
        try:
            r = subprocess.run([tool, "--version"], capture_output=True)
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            pass
    return "iptables"


def _resolve_server_ips(host: str) -> list[str]:
    """
    把用户输入的服务端 (IP / 域名) 展开为 IPv4 字符串列表。
    iptables/nftables 虽然允许域名，但只在规则插入瞬间解析一次；
    域名背后多 IP 或后续变化会导致排除规则失效、流量环路。
    所以在生成脚本前一次性解析，把所有 A 记录写成多条 RETURN 规则。
    """
    if not host:
        return []
    if _looks_like_ip(host):
        return [host]                      # 已是 IP 字面量
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        ips = sorted({info[4][0] for info in infos})
        if len(ips) > 1:
            WARN(f"域名 {host} 解析到 {len(ips)} 个 IP，全部加入 RETURN：{', '.join(ips)}")
        return ips
    except Exception as e:
        ERR(f"无法解析 {host}：{e}（防环路规则将被跳过，请改为填写 IP 重跑）")
        return []


def _tproxy_scripts_ipt(server_ips: list[str], port: int, local_mode: bool,
                         dns_port: int = 0, proxy_uid: int = 0) -> tuple[str, str]:
    a: list[str] = [
        "#!/bin/bash",
        "# PyReality TProxy 规则 - iptables（由 setup.py 自动生成）",
        "set -e",
        "",
        *_ROUTE_CMDS,
        "",
        "# PREROUTING：拦截经本机转发的 TCP 流量（局域网设备）",
        "iptables -t mangle -N PYREALIY",
        *[f"iptables -t mangle -A PYREALIY -d {c} -j RETURN" for c in _PRIVATES],
        (f"iptables -t mangle -A PYREALIY -p tcp"
         f" -j TPROXY --tproxy-mark {_TPROXY_MARK}/{_TPROXY_MARK} --on-port {port}"),
        "iptables -t mangle -A PREROUTING -j PYREALIY",
    ]
    if local_mode:
        a += [
            "",
            "# OUTPUT：拦截本机自身发出的 TCP 流量",
            "iptables -t mangle -N PYREALIY_LOCAL",
            *[f"iptables -t mangle -A PYREALIY_LOCAL -d {c} -j RETURN" for c in _PRIVATES],
        ]
        if server_ips:
            a += ["# 排除代理服务端，防止流量环路"]
            a += [f"iptables -t mangle -A PYREALIY_LOCAL -d {ip} -j RETURN"
                  for ip in server_ips]
        a += [
            (f"iptables -t mangle -A PYREALIY_LOCAL -p tcp"
             f" -j MARK --set-mark {_TPROXY_MARK}/{_TPROXY_MARK}"),
            "iptables -t mangle -A OUTPUT -j PYREALIY_LOCAL",
        ]

    if dns_port:
        a += [
            "",
            f"# DNS 透明捕获：UDP 53 → 本地 DNS 转发器（端口 {dns_port}）",
            "iptables -t nat -N PYREALIY_DNS",
            (f"iptables -t nat -A PYREALIY_DNS -p udp --dport 53"
             f" -j REDIRECT --to-port {dns_port}"),
            "iptables -t nat -A PREROUTING -j PYREALIY_DNS",
        ]
        if local_mode:
            a += [
                "iptables -t nat -N PYREALIY_DNS_LOCAL",
                # 排除代理进程自身，防止 DNS 转发器向上游 DNS 查询时形成环路
                (f"iptables -t nat -A PYREALIY_DNS_LOCAL -p udp --dport 53"
                 f" -m owner --uid-owner {proxy_uid} -j RETURN"),
                (f"iptables -t nat -A PYREALIY_DNS_LOCAL -p udp --dport 53"
                 f" -j REDIRECT --to-port {dns_port}"),
                "iptables -t nat -A OUTPUT -j PYREALIY_DNS_LOCAL",
            ]

    a += ["", "echo '[✓] TProxy 规则已应用（iptables）'"]

    c: list[str] = [
        "#!/bin/bash",
        "# PyReality TProxy 规则清理 - iptables（由 setup.py 自动生成）",
        "",
        "iptables -t mangle -D PREROUTING -j PYREALIY 2>/dev/null || true",
        "iptables -t mangle -F PYREALIY 2>/dev/null || true",
        "iptables -t mangle -X PYREALIY 2>/dev/null || true",
    ]
    if local_mode:
        c += [
            "iptables -t mangle -D OUTPUT -j PYREALIY_LOCAL 2>/dev/null || true",
            "iptables -t mangle -F PYREALIY_LOCAL 2>/dev/null || true",
            "iptables -t mangle -X PYREALIY_LOCAL 2>/dev/null || true",
        ]
    if dns_port:
        c += [
            "iptables -t nat -D PREROUTING -j PYREALIY_DNS 2>/dev/null || true",
            "iptables -t nat -F PYREALIY_DNS 2>/dev/null || true",
            "iptables -t nat -X PYREALIY_DNS 2>/dev/null || true",
        ]
        if local_mode:
            c += [
                "iptables -t nat -D OUTPUT -j PYREALIY_DNS_LOCAL 2>/dev/null || true",
                "iptables -t nat -F PYREALIY_DNS_LOCAL 2>/dev/null || true",
                "iptables -t nat -X PYREALIY_DNS_LOCAL 2>/dev/null || true",
            ]
    c += [*_ROUTE_CLEAN, "", "echo '[✓] TProxy 规则已清除（iptables）'"]

    return "\n".join(a) + "\n", "\n".join(c) + "\n"


def _tproxy_scripts_nft(server_ips: list[str], port: int, local_mode: bool,
                         dns_port: int = 0, proxy_uid: int = 0) -> tuple[str, str]:
    privates = "{ " + ", ".join(_PRIVATES) + " }"

    prerouting = [
        "    chain prerouting {",
        "        type filter hook prerouting priority mangle; policy accept;",
        f"        ip daddr {privates} return",
        f"        meta l4proto tcp tproxy to :{port} meta mark set {_TPROXY_MARK}",
        "    }",
    ]
    output: list[str] = []
    if local_mode:
        output_rules = [f"        ip daddr {privates} return"]
        for ip in server_ips:
            output_rules.append(f"        ip daddr {ip} return")
        output_rules.append(f"        meta l4proto tcp meta mark set {_TPROXY_MARK}")
        output = [
            "    chain output {",
            "        type route hook output priority mangle; policy accept;",
            *output_rules,
            "    }",
        ]

    nft_body = "\n".join(["table ip pyrealiy {", *prerouting, *output, "}"])

    # DNS 捕获使用独立的 nat 表（REDIRECT 只能在 nat 类型 chain 中使用）
    dns_nat_body = ""
    if dns_port:
        dns_pre = [
            "    chain prerouting {",
            "        type nat hook prerouting priority dstnat; policy accept;",
            f"        meta l4proto udp th dport 53 redirect to :{dns_port}",
            "    }",
        ]
        dns_out: list[str] = []
        if local_mode:
            dns_out = [
                "    chain output {",
                "        type nat hook output priority dstnat; policy accept;",
                # 排除代理进程自身，防止 DNS 转发器向上游查询时形成环路
                f"        meta l4proto udp th dport 53 skuid {proxy_uid} return",
                f"        meta l4proto udp th dport 53 redirect to :{dns_port}",
                "    }",
            ]
        dns_nat_body = "\n".join(["table ip pyrealiy_nat {", *dns_pre, *dns_out, "}"])

    a: list[str] = [
        "#!/bin/bash",
        "# PyReality TProxy 规则 - nftables（由 setup.py 自动生成）",
        "set -e",
        "",
        *_ROUTE_CMDS,
        "",
        "# nftables 规则",
        "nft -f - << 'NFTEOF'",
        nft_body,
    ]
    if dns_nat_body:
        a += [dns_nat_body]
    a += [
        "NFTEOF",
        "",
        "echo '[✓] TProxy 规则已应用（nftables）'",
    ]

    c: list[str] = [
        "#!/bin/bash",
        "# PyReality TProxy 规则清理 - nftables（由 setup.py 自动生成）",
        "",
        "nft delete table ip pyrealiy 2>/dev/null || true",
    ]
    if dns_port:
        c += ["nft delete table ip pyrealiy_nat 2>/dev/null || true"]
    c += [
        *_ROUTE_CLEAN,
        "",
        "echo '[✓] TProxy 规则已清除（nftables）'",
    ]

    return "\n".join(a) + "\n", "\n".join(c) + "\n"


def _tproxy_scripts(server_ips: list[str], port: int, local_mode: bool, backend: str,
                     dns_port: int = 0, proxy_uid: int = 0) -> tuple[str, str]:
    if backend == "nftables":
        return _tproxy_scripts_nft(server_ips, port, local_mode, dns_port, proxy_uid)
    return _tproxy_scripts_ipt(server_ips, port, local_mode, dns_port, proxy_uid)


def configure_tproxy(server_ip: str, cfg: dict) -> None:
    """询问是否启用 TProxy，生成防火墙脚本，可选立即应用。"""
    print()
    if not ask_yn("是否启用 TProxy 透明代理（需要 root / CAP_NET_ADMIN）？", default=False):
        return

    tproxy_port = ask_port("TProxy 监听端口", "7893")
    cfg["tproxy_port"] = tproxy_port

    mode = ask(
        "代理范围：[1] 全局（本机流量 + 局域网转发）  [2] 仅局域网转发",
        "1",
    )
    local_mode = mode.strip() != "2"
    if local_mode:
        INFO("全局模式：本机发往代理服务端的流量将自动排除，防止环路")

    detected  = _detect_firewall()
    def_fw    = "2" if detected == "nftables" else "1"
    INFO(f"检测到系统防火墙工具：{detected}")
    fw_choice = ask("规则工具：[1] iptables  [2] nftables", def_fw)
    backend   = "nftables" if fw_choice.strip() == "2" else "iptables"

    dns_port  = 0
    proxy_uid = os.getuid()
    if cfg.get("dns_listen_port", 0):
        if ask_yn(f"同时透明捕获 DNS（UDP 53 → 本地转发器端口 {cfg['dns_listen_port']}）？"):
            dns_port = cfg["dns_listen_port"]
            # tproxy 模式下 DNS 转发器需要监听所有接口以接收 LAN 设备的查询
            cfg["dns_listen_host"] = "0.0.0.0"
            INFO(f"DNS 透明捕获已启用（owner uid={proxy_uid} 的进程将绕过重定向，防止环路）")

    # 用户可能填了域名；iptables/nftables 只在插入时解析一次，
    # 多 IP 或后续变化会破规则。提前解析为 IP 列表，多 IP 全部排除。
    server_ips = _resolve_server_ips(server_ip)

    apply_content, cleanup_content = _tproxy_scripts(
        server_ips, tproxy_port, local_mode, backend, dns_port, proxy_uid
    )

    apply_path   = "tproxy_rules.sh"
    cleanup_path = "tproxy_cleanup.sh"

    with open(apply_path, "w") as f:
        f.write(apply_content)
    os.chmod(apply_path, 0o755)

    with open(cleanup_path, "w") as f:
        f.write(cleanup_content)
    os.chmod(cleanup_path, 0o755)

    OK(f"{backend} 脚本已写入：{apply_path}（应用）/ {cleanup_path}（清理）")
    INFO(f"应用规则：sudo bash {apply_path}")
    INFO(f"清除规则：sudo bash {cleanup_path}")

    if local_mode:
        INFO("为局域网设备代理还需开启 IP 转发：")
        print(f"    {_c('36', 'echo 1 > /proc/sys/net/ipv4/ip_forward')}")

    if check_root() and ask_yn("是否立即应用这些规则？"):
        r = subprocess.run(["bash", apply_path])
        if r.returncode == 0:
            OK("TProxy 规则已应用")
        else:
            ERR("应用失败，请手动执行脚本")


def configure_client() -> None:
    TITLE("客户端配置")

    server_host = ask_ip_or_domain("服务端 IP 或域名")
    server_port = ask_port("服务端端口", "443")
    password    = ask("连接密码（与服务端一致）")
    camouflage  = ask_camouflage_host("伪装域名（与服务端一致）", "www.apple.com")
    socks5_port = ask_port("本地 SOCKS5 端口", "1080")

    # ── DNS 转发器 ────────────────────────────────────────────────────────────
    dns_cfg: dict = {}
    if ask_yn("是否启用本地 DNS 转发器（防止 DNS 泄漏）？"):
        dns_port = ask_port("DNS 监听端口（5353 无需 root，53 需要 root）", "5353")
        cn_dns   = ask_ip("国内 DNS 服务器", "223.5.5.5")
        remote   = ask_ip("境外 DNS 服务器（通过隧道解析）", "8.8.8.8")
        dns_cfg  = {
            "dns_listen_host": "127.0.0.1",
            "dns_listen_port": dns_port,
            "cn_dns":          cn_dns,
            "remote_dns":      remote,
        }
        INFO(f"国内域名 → {cn_dns}  境外域名 → {remote}（通过隧道）")
        INFO(f"启动后将系统 DNS 改为 127.0.0.1:{dns_port} 即可防止泄漏")
    else:
        dns_cfg = {"dns_listen_port": 0}

    rules_cfg = configure_rules()

    cfg = {
        "socks5_host":    "0.0.0.0",
        "socks5_port":    socks5_port,
        "server_host":    server_host,
        "server_port":    server_port,
        "password":       password,
        "camouflage_host": camouflage,
        "brutal_rate_bps": 0,
        "brutal_pool_size": 10,
        **dns_cfg,
        **rules_cfg,
    }

    configure_tproxy(server_host, cfg)

    path = "config_client.json"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
    OK(f"客户端配置已写入 {path}")


# ── 系统服务安装 ──────────────────────────────────────────────────────────────

def _detect_init_system() -> str:
    """检测当前 init 系统，返回 'systemd' / 'openrc' / 'sysvinit'"""
    try:
        with open("/proc/1/comm") as f:
            if "systemd" in f.read():
                return "systemd"
    except OSError:
        pass
    # Alpine Linux / OpenRC
    if os.path.exists("/sbin/openrc-run") or os.path.exists("/etc/alpine-release"):
        return "openrc"
    try:
        r = subprocess.run(["systemctl", "--version"], capture_output=True)
        if r.returncode == 0:
            return "systemd"
    except FileNotFoundError:
        pass
    return "sysvinit"


def _systemd_unit(name: str, desc: str, python: str,
                  script: str, config: str, work_dir: str) -> str:
    # systemd 用 LimitNOFILE 设置文件描述符上限，等价于 ulimit -n
    return (
        f"[Unit]\n"
        f"Description={desc}\n"
        f"After=network-online.target\n"
        f"Wants=network-online.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"WorkingDirectory={work_dir}\n"
        f"ExecStart={python} {script} {config}\n"
        f"Restart=on-failure\n"
        f"RestartSec=5\n"
        f"LimitNOFILE=65536\n"
        f"StandardOutput=journal\n"
        f"StandardError=journal\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def _sysvinit_script(name: str, desc: str, python: str,
                     script: str, config: str, work_dir: str) -> str:
    return (
        f"#!/bin/bash\n"
        f"### BEGIN INIT INFO\n"
        f"# Provides:          {name}\n"
        f"# Required-Start:    $network $remote_fs $syslog\n"
        f"# Required-Stop:     $network $remote_fs $syslog\n"
        f"# Default-Start:     2 3 4 5\n"
        f"# Default-Stop:      0 1 6\n"
        f"# Short-Description: {desc}\n"
        f"### END INIT INFO\n"
        f"\n"
        f'NAME="{name}"\n'
        f'PYTHON="{python}"\n'
        f'SCRIPT="{script}"\n'
        f'CONFIG="{config}"\n'
        f'WORKDIR="{work_dir}"\n'
        f'PIDFILE="/var/run/${{NAME}}.pid"\n'
        f'LOGFILE="/var/log/${{NAME}}.log"\n'
        f"\n"
        f"start() {{\n"
        f'    echo -n "Starting ${{NAME}}: "\n'
        f'    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then\n'
        f'        echo "already running"; return 0\n'
        f"    fi\n"
        f"    ulimit -n 65536\n"
        f'    cd "$WORKDIR"\n'
        f'    nohup "$PYTHON" "$SCRIPT" "$CONFIG" >> "$LOGFILE" 2>&1 &\n'
        f'    echo $! > "$PIDFILE"\n'
        f'    echo "OK"\n'
        f"}}\n"
        f"\n"
        f"stop() {{\n"
        f'    echo -n "Stopping ${{NAME}}: "\n'
        f'    if [ ! -f "$PIDFILE" ]; then echo "not running"; return 0; fi\n'
        f'    kill "$(cat $PIDFILE)" 2>/dev/null && rm -f "$PIDFILE"\n'
        f'    echo "OK"\n'
        f"}}\n"
        f"\n"
        f"status() {{\n"
        f'    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then\n'
        f'        echo "${{NAME}} is running (pid $(cat $PIDFILE))"\n'
        f"    else\n"
        f'        echo "${{NAME}} is not running"\n'
        f'        [ -f "$PIDFILE" ] && rm -f "$PIDFILE"\n'
        f"    fi\n"
        f"}}\n"
        f"\n"
        f'case "$1" in\n'
        f"    start)   start   ;;\n"
        f"    stop)    stop    ;;\n"
        f"    restart) stop; sleep 1; start ;;\n"
        f"    status)  status  ;;\n"
        f"    *)\n"
        f'        echo "Usage: $0 {{start|stop|restart|status}}"\n'
        f"        exit 1 ;;\n"
        f"esac\n"
        f"exit 0\n"
    )


def _openrc_script(name: str, desc: str, python: str,
                   script: str, config: str, work_dir: str) -> str:
    # openrc-run 原生支持 command_background、pidfile、output_log
    # start_pre() 在进程启动前执行，ulimit 在此设置可被子进程继承
    return (
        f"#!/sbin/openrc-run\n"
        f"\n"
        f'name="{name}"\n'
        f'description="{desc}"\n'
        f'command="{python}"\n'
        f'command_args="{script} {config}"\n'
        f"command_background=true\n"
        f'directory="{work_dir}"\n'
        f'pidfile="/var/run/${{RC_SVCNAME}}.pid"\n'
        f'output_log="/var/log/${{RC_SVCNAME}}.log"\n'
        f'error_log="/var/log/${{RC_SVCNAME}}.log"\n'
        f"\n"
        f"depend() {{\n"
        f"    need net\n"
        f"    after firewall\n"
        f"}}\n"
        f"\n"
        f"start_pre() {{\n"
        f"    ulimit -n 65536\n"
        f"}}\n"
    )


def _install_openrc(name: str, dest: str, content: str) -> None:
    if check_root():
        try:
            with open(dest, "w") as f:
                f.write(content)
            os.chmod(dest, 0o755)
            subprocess.run(["rc-update", "add", name, "default"], check=True, capture_output=True)
            OK(f"服务已安装：{dest}")
            OK(f"{name} 已加入 default runlevel（开机自启）")
            INFO(f"启动服务：rc-service {name} start")
            INFO(f"查看状态：rc-service {name} status")
            INFO(f"实时日志：tail -f /var/log/{name}.log")
            print()
            if ask_yn("是否立即启动服务？"):
                subprocess.run(["rc-service", name, "start"])
        except Exception as e:
            ERR(f"安装失败：{e}")
    else:
        local = f"{name}.openrc"
        with open(local, "w") as f:
            f.write(content)
        os.chmod(local, 0o755)
        OK(f"OpenRC 脚本已写到当前目录：{local}")
        INFO("需要 root 权限，请手动执行：")
        for cmd in [
            f"sudo cp {local} {dest}",
            f"sudo chmod +x {dest}",
            f"sudo rc-update add {name} default",
            f"sudo rc-service {name} start",
        ]:
            print(f"    {_c('36', cmd)}")


def _install_systemd(name: str, dest: str, content: str) -> None:
    if check_root():
        try:
            with open(dest, "w") as f:
                f.write(content)
            subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
            subprocess.run(["systemctl", "enable", name],  check=True, capture_output=True)
            OK(f"服务已安装：{dest}")
            OK(f"{name} 已设置为开机自启")
            INFO(f"启动服务：systemctl start {name}")
            INFO(f"查看状态：systemctl status {name}")
            INFO(f"实时日志：journalctl -u {name} -f")
            print()
            if ask_yn("是否立即启动服务？"):
                subprocess.run(["systemctl", "start", name])
        except Exception as e:
            ERR(f"安装失败：{e}")
    else:
        local = f"{name}.service"
        with open(local, "w") as f:
            f.write(content)
        OK(f"service 文件已写到当前目录：{local}")
        INFO("需要 root 权限，请手动执行：")
        for cmd in [
            f"sudo cp {local} {dest}",
            "sudo systemctl daemon-reload",
            f"sudo systemctl enable {name}",
            f"sudo systemctl start {name}",
        ]:
            print(f"    {_c('36', cmd)}")


def _install_sysvinit(name: str, dest: str, content: str) -> None:
    if check_root():
        try:
            with open(dest, "w") as f:
                f.write(content)
            os.chmod(dest, 0o755)
            # 尝试 update-rc.d（Debian/Ubuntu）或 chkconfig（RHEL/CentOS）
            enabled = False
            for enabler in [["update-rc.d", name, "defaults"],
                            ["chkconfig", "--add", name]]:
                try:
                    r = subprocess.run(enabler, capture_output=True)
                    if r.returncode == 0:
                        OK(f"已通过 {enabler[0]} 注册开机自启")
                        enabled = True
                        break
                except FileNotFoundError:
                    pass
            if not enabled:
                WARN("未找到 update-rc.d / chkconfig，请手动配置开机自启")
            OK(f"init 脚本已安装：{dest}")
            INFO(f"启动服务：service {name} start")
            INFO(f"查看状态：service {name} status")
            INFO(f"实时日志：tail -f /var/log/{name}.log")
            print()
            if ask_yn("是否立即启动服务？"):
                subprocess.run(["service", name, "start"])
        except Exception as e:
            ERR(f"安装失败：{e}")
    else:
        local = f"{name}.init"
        with open(local, "w") as f:
            f.write(content)
        os.chmod(local, 0o755)
        OK(f"init 脚本已写到当前目录：{local}")
        INFO("需要 root 权限，请手动执行：")
        for cmd, comment in [
            (f"sudo cp {local} {dest}", ""),
            (f"sudo chmod +x {dest}", ""),
            (f"sudo update-rc.d {name} defaults", "Debian/Ubuntu"),
            (f"sudo chkconfig --add {name}", "RHEL/CentOS"),
            (f"sudo service {name} start", ""),
        ]:
            suffix = f"  # {comment}" if comment else ""
            print(f"    {_c('36', cmd)}{_c('90', suffix)}")


def install_service(role: str, config_path: str) -> None:
    """引导用户将服务端或客户端安装为系统服务。"""
    if not check_linux():
        return
    print()
    if not ask_yn("是否安装为系统服务（开机自动启动）？", default=False):
        return

    work_dir   = os.path.abspath(".")
    python     = sys.executable
    script     = os.path.join(work_dir, f"{role}.py")
    config_abs = os.path.abspath(config_path)
    name       = f"pyrealiy-{role}"
    desc       = "PyReality 代理服务端" if role == "server" else "PyReality 代理客户端"

    detected   = _detect_init_system()
    def_choice = {"systemd": "1", "openrc": "3"}.get(detected, "2")
    INFO(f"检测到 init 系统：{detected}")
    choice = ask(
        "安装方式：[1] systemd  [2] SysV init（/etc/init.d）  [3] OpenRC（Alpine）",
        def_choice,
    )
    choice = choice.strip()

    if choice == "3":
        content = _openrc_script(name, desc, python, script, config_abs, work_dir)
        dest    = f"/etc/init.d/{name}"
        _install_openrc(name, dest, content)
    elif choice == "2":
        content = _sysvinit_script(name, desc, python, script, config_abs, work_dir)
        dest    = f"/etc/init.d/{name}"
        _install_sysvinit(name, dest, content)
    else:
        content = _systemd_unit(name, desc, python, script, config_abs, work_dir)
        dest    = f"/etc/systemd/system/{name}.service"
        _install_systemd(name, dest, content)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    TITLE("PyReality 部署向导")

    role = ask("部署角色：[1] 服务端（墙外 VPS）  [2] 客户端（本地）", "1")

    if role == "2":
        configure_client()
        install_service("client", "config_client.json")
        TITLE("启动命令")
        print("  python3 client.py config_client.json")
    else:
        # 仅服务端需要 TCP Brutal 内核模块
        brutal_ok = handle_brutal_install()
        configure_server(brutal_ok)
        install_service("server", "config_server.json")
        TITLE("启动命令")
        print("  python3 server.py config_server.json")

    print()
    INFO("依赖安装：pip install cryptography uvloop")
    print()


if __name__ == "__main__":
    main()
