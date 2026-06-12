"""
TCP Brutal 拥塞控制支持

TCP Brutal 以固定速率发送，忽略丢包导致的拥塞退让，
适合高丢包率的跨境链路（普通 TCP 在丢包时会反复降速）。

【安装内核模块（服务端和客户端都需要）】
  # 方式1：从源码编译
  git clone https://github.com/apernet/tcp-brutal
  cd tcp-brutal && make && insmod tcp_brutal.ko

  # 方式2：DKMS（推荐，重启后自动加载）
  apt install dkms
  # 然后按 README 操作

【验证是否可用】
  python3 -c "from core.brutal import is_available; print(is_available())"

【速率建议】
  设置为你的实际带宽的 1.5~2 倍，让 Brutal 有足够余量填满信道。
  例如带宽 50 Mbps → 设 brutal_rate_mbps: 80
"""


from __future__ import annotations

import socket

import struct


from .utils import get_logger

logger = get_logger("brutal")

# Linux TCP socket 选项常量
_TCP_CONGESTION  = 13      # 设置拥塞控制算法名称
_TCP_BRUTAL_PARAMS = 23301 # tcp_brutal 模块定义的速率参数选项

# BrutalParams 结构体（与内核模块 ABI 对齐）：
#   uint64 rate      —— 字节/秒
#   uint32 cwnd_gain —— cwnd 放大倍数 × 10
#
# cwnd_gain = 15（1.5x）：参考实现中的推荐值，保守稳定
# cwnd_gain = 40（4.0x）：更激进，适合极高丢包但可能引起突发
_BRUTAL_PARAMS_FMT = "QI"   # 原生字节序（Linux x86/arm 均为 little-endian）
_DEFAULT_CWND_GAIN = 15     # 对齐参考实现，1.5x 窗口放大


def is_available() -> bool:
    """检查当前系统是否支持 TCP Brutal（内核模块已加载）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.IPPROTO_TCP, _TCP_CONGESTION, "brutal".encode())
            return True
    except OSError:
        return False


def set_algorithm(sock: socket.socket) -> bool:
    """
    仅设置拥塞控制算法为 brutal，不设速率。
    用于监听 socket：accept() 出的子连接会自动继承算法名称，
    之后只需对每条子连接单独调用 set_rate()。
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, _TCP_CONGESTION, "brutal".encode())
        return True
    except OSError as e:
        logger.debug("Cannot set brutal algorithm: %s", e)
        return False


def set_rate(sock: socket.socket, rate_bps: int, cwnd_gain: int = _DEFAULT_CWND_GAIN) -> bool:
    """
    在已有连接的 socket 上设置 Brutal 速率参数。
    通常在 accept() 之后调用，此时算法名已由监听 socket 继承。

    参数：
      rate_bps  : 目标发送速率（字节/秒）
      cwnd_gain : 拥塞窗口放大系数 × 10，默认 15（= 1.5x）
    """
    try:
        params = struct.pack(_BRUTAL_PARAMS_FMT, rate_bps, cwnd_gain)
        sock.setsockopt(socket.IPPROTO_TCP, _TCP_BRUTAL_PARAMS, params)
        logger.debug("Brutal rate set: %.1f Mbps (cwnd_gain=%.1fx)",
                     rate_bps / 1_000_000, cwnd_gain / 10)
        return True
    except OSError as e:
        logger.debug("Cannot set brutal rate: %s", e)
        return False


def enable(sock: socket.socket, rate_bps: int, cwnd_gain: int = _DEFAULT_CWND_GAIN) -> bool:
    """
    在同一个 socket 上同时设置算法名和速率（用于客户端出站连接）。
    返回 True 表示成功，False 静默回退到普通 TCP。
    """
    ok = set_algorithm(sock) and set_rate(sock, rate_bps, cwnd_gain)
    if ok:
        logger.info("TCP Brutal enabled: %.1f Mbps, cwnd_gain=%.1fx",
                    rate_bps / 1_000_000, cwnd_gain / 10)
    return ok


async def open_brutal_connection(
    host: str,
    port: int,
    rate_bps: int,
) -> tuple:
    """
    建立 TCP 连接并启用 Brutal，返回 (reader, writer)。

    asyncio.open_connection() 不允许在连接前设置 socket 选项，
    所以这里手动创建 socket → 设置 Brutal → 连接 → 包装成 asyncio 流。
    """
    import asyncio

    loop = asyncio.get_event_loop()

    # 解析域名（支持 IPv4 / IPv6）
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"Cannot resolve {host}")

    af, socktype, proto, _, sockaddr = infos[0]
    sock = socket.socket(af, socktype, proto)
    sock.setblocking(False)

    # 在连接前设置 Brutal（内核会在握手后开始生效）
    enable(sock, rate_bps)

    await loop.sock_connect(sock, sockaddr)
    reader, writer = await asyncio.open_connection(sock=sock, limit=262144)
    return reader, writer
