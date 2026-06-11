"""
TProxy 透明代理监听器（仅限 Linux）

需要：
  - root 权限或 CAP_NET_ADMIN capability
  - iptables TPROXY 规则将目标流量重定向到此端口（参考 README "TProxy 防火墙规则" 模板）

连接到达时 writer.get_extra_info('sockname') 直接返回原始目标地址，
无需任何协议握手，与 SOCKS5 处理逻辑完全兼容。
"""

from __future__ import annotations

import asyncio
import socket

from .utils import get_logger

logger = get_logger("tproxy")

_IP_TRANSPARENT = 19  # Linux-specific


def _make_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, _IP_TRANSPARENT, 1)
    except OSError as e:
        sock.close()
        raise OSError(
            f"IP_TRANSPARENT 设置失败（需要 root 或 CAP_NET_ADMIN）: {e}"
        ) from e
    sock.setblocking(False)
    sock.bind((host, port))
    sock.listen(256)
    return sock


async def start_server(host: str, port: int, handler) -> asyncio.Server:
    """创建绑定到 IP_TRANSPARENT socket 的 asyncio 服务器。"""
    sock = _make_socket(host, port)
    server = await asyncio.start_server(handler, sock=sock)
    logger.info("TProxy listening on %s:%d", host, port)
    return server
