"""
TLS 伪装模块

服务端收到 TCP 连接后先读取 ClientHello，检查 legacy_session_id：

  ┌─ 含有效认证 token ──► 直接建代理信道（零额外延迟）
  │
  └─ 普通/无效 token  ──► 从本地缓存回放握手记录（零额外延迟）
                           探测器收到真实 apple.com 证书

两条路径对 GFW 时延测量都不留指纹。
"""


from __future__ import annotations

import asyncio

from .hello_auth import extract_session_id, verify_session_token

from .handshake_cache import HandshakeCache

from .utils import get_logger

logger = get_logger("camouflage")


async def _read_tls_record(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    length = int.from_bytes(header[3:5], "big")
    if length > 16384 + 256:
        raise ValueError(f"TLS record too large: {length}")
    body = await reader.readexactly(length)
    return header[0], header + body


async def server_read_hello_and_decide(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    password: str,
    cache: HandshakeCache,
) -> bool:
    """
    读取 ClientHello，验证 session_id 中的认证 token。

    合法客户端 → 返回 True，调用方继续建代理信道。
    探测/非法  → 从缓存回放握手，连接由本函数负责关闭，返回 False。
    """
    try:
        _ct, hello_raw = await asyncio.wait_for(_read_tls_record(client_reader), timeout=8.0)
    except Exception as e:
        logger.debug("Failed to read ClientHello: %s", e)
        return False

    session_id = extract_session_id(hello_raw)
    if session_id and len(session_id) == 32 and verify_session_token(password, session_id):
        logger.info("In-Hello auth OK")
        return True

    # 探测连接：从缓存本地回放，不产生任何跨网络延迟
    logger.debug("Probe or unauthorized connection, replying from cache")
    await cache.serve_probe(client_reader, client_writer)
    return False
