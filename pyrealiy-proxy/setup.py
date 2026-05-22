#!/usr/bin/env python3
"""
PyReality 交互式部署脚本

自动检测系统环境、安装 TCP Brutal 内核模块、生成配置文件。
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import struct
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

def install_brutal_dkms() -> bool:
    """通过 DKMS 安装 tcp_brutal（持久化，重启后仍有效）"""
    INFO("尝试通过 DKMS 安装 tcp_brutal ...")
    steps = [
        (["apt-get", "update", "-q"],                           "更新软件源"),
        (["apt-get", "install", "-y", "dkms", "linux-headers-$(uname -r)"],
                                                                "安装 DKMS 和内核头文件"),
        (["git", "clone", "--depth=1",
          "https://github.com/apernet/tcp-brutal", "/tmp/tcp-brutal"],
                                                                "克隆源码"),
        (["bash", "-c", "cd /tmp/tcp-brutal && make dkms"],     "DKMS 安装模块"),
        (["modprobe", "tcp_brutal"],                            "加载模块"),
    ]
    for cmd, desc in steps:
        INFO(desc)
        r = _run(cmd if not any("$(" in c for c in cmd) else cmd,
                 shell=("$(" in " ".join(cmd)))
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
        "安装方式：[1] DKMS（持久化，推荐）  [2] 临时加载",
        default="1",
    )
    if method == "2":
        ok = install_brutal_simple()
    else:
        ok = install_brutal_dkms()

    if ok and brutal_is_loaded():
        OK("TCP Brutal 安装成功")
        return True
    else:
        ERR("安装失败，将使用普通 TCP")
        return False


# ── 配置生成 ──────────────────────────────────────────────────────────────────

def configure_server(brutal_available: bool) -> None:
    TITLE("服务端配置")

    listen_port   = ask("监听端口", "443")
    password      = ask("连接密码（建议用随机字符串）")
    camouflage    = ask("伪装域名", "www.apple.com")

    rate_bps = 0
    if brutal_available:
        iface = get_default_interface()
        speed = get_interface_speed_mbps(iface)
        suggested = suggest_brutal_rate(iface)
        INFO(f"检测到网卡 {iface}，带宽 {speed} Mbps")
        INFO(f"建议单连接速率：{suggested // 1_000_000} Mbps")
        if ask_yn("启用 TCP Brutal？"):
            rate_input = ask(
                f"单连接 Brutal 速率（Mbps）",
                str(suggested // 1_000_000),
            )
            rate_bps = int(rate_input) * 1_000_000
    else:
        INFO("TCP Brutal 不可用，使用普通 TCP")

    cfg = {
        "listen_host":    "0.0.0.0",
        "listen_port":    int(listen_port),
        "password":       password,
        "camouflage_host": camouflage,
        "camouflage_port": 443,
        "brutal_rate_bps": rate_bps,
    }
    path = "config_server.json"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
    OK(f"服务端配置已写入 {path}")


def configure_client(brutal_available: bool) -> None:
    TITLE("客户端配置")

    server_host = ask("服务端 IP 或域名")
    server_port = ask("服务端端口", "443")
    password    = ask("连接密码（与服务端一致）")
    camouflage  = ask("伪装域名（与服务端一致）", "www.apple.com")
    socks5_port = ask("本地 SOCKS5 端口", "1080")

    rate_bps  = 0
    pool_size = 10

    if brutal_available:
        iface = get_default_interface()
        suggested = suggest_brutal_rate(iface)
        INFO(f"建议单连接速率：{suggested // 1_000_000} Mbps")
        if ask_yn("启用 TCP Brutal 多连接加速？"):
            rate_input = ask("单连接速率（Mbps）", str(suggested // 1_000_000))
            rate_bps = int(rate_input) * 1_000_000

            pool_input = ask(
                "预建连接数（越多总吞吐越高，建议 10~20）",
                "20",
            )
            pool_size = int(pool_input)

            total_mbps = rate_bps * pool_size / 1e6
            INFO(f"预计总吞吐上限：{pool_size} × {rate_bps//1_000_000} = {total_mbps:.0f} Mbps")

    cfg = {
        "socks5_host":    "127.0.0.1",
        "socks5_port":    int(socks5_port),
        "server_host":    server_host,
        "server_port":    int(server_port),
        "password":       password,
        "camouflage_host": camouflage,
        "brutal_rate_bps": rate_bps,
        "brutal_pool_size": pool_size,
    }
    path = "config_client.json"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
    OK(f"客户端配置已写入 {path}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    TITLE("PyReality 部署向导")

    role = ask("部署角色：[1] 服务端（墙外 VPS）  [2] 客户端（本地）", "1")

    # 检测并（可选）安装 TCP Brutal
    brutal_ok = handle_brutal_install()

    if role == "2":
        configure_client(brutal_ok)
        TITLE("启动命令")
        print("  python3 client.py config_client.json")
    else:
        configure_server(brutal_ok)
        TITLE("启动命令")
        print("  python3 server.py config_server.json")

    print()
    INFO("依赖安装：pip install cryptography")
    print()


if __name__ == "__main__":
    main()
