"""
SOCKS5 协议解析 —— 客户端本地监听端口

支持的命令：
  CONNECT (0x01) —— TCP 代理
  UDP ASSOCIATE (0x03) —— UDP 转发（0.4.10 起；详见 core/udp_relay.py）

不支持 BIND (0x02)。不实现用户名密码认证（本地用，不需要）。

SOCKS5 握手流程：
  客户端 → [VER=5][NMETHODS][METHODS...]
  服务端 ← [VER=5][METHOD=0(无认证)]
  客户端 → [VER=5][CMD][RSV=0][ATYP][DST.ADDR][DST.PORT]
  服务端 ← [VER=5][REP=0][RSV=0][ATYP][BND.ADDR][BND.PORT]
"""


from __future__ import annotations

import asyncio

import socket as _socket

import struct

from .utils import get_logger

logger = get_logger("socks5")

SOCKS5_VER         = 5
CMD_CONNECT        = 1
CMD_UDP_ASSOCIATE  = 3
ATYP_IPV4          = 1
ATYP_DOMAIN        = 3
ATYP_IPV6          = 4

# REP 字段（SOCKS5 RFC 1928 §6）
REP_SUCCESS                 = 0x00
REP_GENERAL_FAILURE         = 0x01
REP_COMMAND_NOT_SUPPORTED   = 0x07


async def parse_socks5_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[str, str, int] | None:
    """
    完成 SOCKS5 握手并解析请求。

    返回 (cmd, host, port)：
      cmd = "tcp"   → CONNECT 命令；调用方应直接走 TCP 代理（已自动回 REP=0）
      cmd = "udp"   → UDP ASSOCIATE 命令；**回 REP 由调用方负责**
                     （调用方需先 bind 本地 UDP socket 才知道 BND.ADDR/PORT）
    失败返回 None。

    对 UDP 路径而言，DST.ADDR/PORT 是 SOCKS5 client 想用来发 UDP 的源地址，
    通常是 0.0.0.0:0（让 server 接受任意源）。我们的 relay 默认接受
    SOCKS5 控制连接同 IP 来的 UDP。
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
    if ver != SOCKS5_VER:
        return None
    if cmd not in (CMD_CONNECT, CMD_UDP_ASSOCIATE):
        await _reply_error(writer, REP_COMMAND_NOT_SUPPORTED)
        return None

    # 解析目标 / 客户端源地址
    if atyp == ATYP_IPV4:
        addr_bytes = await reader.readexactly(4)
        host = ".".join(str(b) for b in addr_bytes)
    elif atyp == ATYP_DOMAIN:
        domain_len = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(domain_len)).decode()
    elif atyp == ATYP_IPV6:
        addr_bytes = await reader.readexactly(16)
        host = _socket.inet_ntop(_socket.AF_INET6, addr_bytes)
    else:
        return None

    port_bytes = await reader.readexactly(2)
    port = struct.unpack("!H", port_bytes)[0]

    if cmd == CMD_CONNECT:
        # 回复"成功"（BND 地址填 0；多数客户端不关心 BND for CONNECT）
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        logger.debug("SOCKS5 TCP -> %s:%d", host, port)
        return "tcp", host, port

    # UDP ASSOCIATE：不在此处回复，由调用方 bind UDP 后再回
    logger.debug("SOCKS5 UDP ASSOCIATE (client source hint %s:%d)", host, port)
    return "udp", host, port


async def reply_udp_associate(writer: asyncio.StreamWriter,
                              bnd_host: str, bnd_port: int) -> None:
    """
    UDP ASSOCIATE 完成后回 SOCKS5 客户端：告知 BND.ADDR / BND.PORT 用来发 UDP。
    """
    pkt = bytearray(b"\x05\x00\x00")   # VER, REP=success, RSV
    try:
        addr = _socket.inet_aton(bnd_host)
        pkt += b"\x01" + addr
    except OSError:
        try:
            addr6 = _socket.inet_pton(_socket.AF_INET6, bnd_host)
            pkt += b"\x04" + addr6
        except OSError:
            hb = bnd_host.encode("ascii", errors="replace")
            pkt += b"\x03" + bytes([len(hb) & 0xff]) + hb
    pkt += struct.pack("!H", bnd_port)
    writer.write(bytes(pkt))
    await writer.drain()


async def _reply_error(writer: asyncio.StreamWriter, rep: int) -> None:
    writer.write(bytes([0x05, rep, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
    try:
        await writer.drain()
    except Exception:
        pass
