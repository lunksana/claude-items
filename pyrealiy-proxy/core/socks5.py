"""
SOCKS5 协议解析 —— 客户端本地监听端口

只实现 CONNECT 命令（TCP 代理），不支持 BIND/UDP。
不实现用户名密码认证（本地用，不需要）。

SOCKS5 握手流程：
  客户端 → [VER=5][NMETHODS][METHODS...]
  服务端 ← [VER=5][METHOD=0(无认证)]
  客户端 → [VER=5][CMD=1][RSV=0][ATYP][DST.ADDR][DST.PORT]
  服务端 ← [VER=5][REP=0][RSV=0][ATYP][BND.ADDR][BND.PORT]
"""


from __future__ import annotations

import asyncio

import struct

from .utils import get_logger

logger = get_logger("socks5")

SOCKS5_VER = 5
CMD_CONNECT = 1
ATYP_IPV4   = 1
ATYP_DOMAIN = 3
ATYP_IPV6   = 4


async def parse_socks5_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[str, int] | None:
    """
    完成 SOCKS5 握手并解析目标地址。
    返回 (host, port)，失败返回 None。
    """
    # 阶段1：方法协商
    data = await reader.readexactly(2)
    ver, nmethods = data[0], data[1]
    if ver != SOCKS5_VER:
        return None
    await reader.readexactly(nmethods)  # 忽略 methods
    writer.write(b"\x05\x00")           # 选择"无认证"
    await writer.drain()

    # 阶段2：请求
    header = await reader.readexactly(4)
    ver, cmd, _, atyp = header
    if ver != SOCKS5_VER or cmd != CMD_CONNECT:
        writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")  # Command not supported
        await writer.drain()
        return None

    # 解析目标地址
    if atyp == ATYP_IPV4:
        addr_bytes = await reader.readexactly(4)
        host = ".".join(str(b) for b in addr_bytes)
    elif atyp == ATYP_DOMAIN:
        domain_len = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(domain_len)).decode()
    elif atyp == ATYP_IPV6:
        addr_bytes = await reader.readexactly(16)
        import socket
        host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
    else:
        return None

    port_bytes = await reader.readexactly(2)
    port = struct.unpack("!H", port_bytes)[0]

    # 回复"成功"（BND 地址填 0）
    writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    await writer.drain()

    logger.info("SOCKS5 → %s:%d", host, port)
    return host, port
