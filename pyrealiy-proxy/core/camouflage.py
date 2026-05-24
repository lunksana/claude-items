"""
TLS 伪装模块

服务端收到 TCP 连接后先读取 ClientHello，检查 legacy_session_id：

  ┌─ 含有效认证 token ──► 服务端先回放缓存的握手记录（ServerHello…ServerHelloDone），
  │                        读取客户端的假 CKE+CCS+Finished，再发假 ServerCCS+Finished，
  │                        派生会话密钥，返回 (True, client_random)
  │
  └─ 普通/无效 token  ──► 从本地缓存回放握手记录，探测器收到真实 apple.com 证书，
                           返回 (False, None)

两条路径在握手阶段产生的字节序列对旁观者完全相同，直到握手完成后应用数据才有差异。
"""

from __future__ import annotations

import asyncio
import os
import struct

from .hello_auth import extract_session_id, extract_client_random, verify_session_token
from .handshake_cache import HandshakeCache
from .utils import get_logger

logger = get_logger("camouflage")


def _fake_server_tail() -> bytes:
    """生成假的 ServerChangeCipherSpec + ServerFinished（Encrypted Handshake Message）"""
    ccs = b"\x14\x03\x03\x00\x01\x01"
    body = os.urandom(40)  # 加密后的 Finished 消息，长度与真实值相近
    fin = b"\x16\x03\x03" + struct.pack("!H", len(body)) + body
    return ccs + fin


async def _read_tls_record(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    length = int.from_bytes(header[3:5], "big")
    if length > 16384 + 256:
        raise ValueError(f"TLS record too large: {length}")
    body = await reader.readexactly(length)
    return header[0], header + body


async def _read_client_tail(reader: asyncio.StreamReader) -> None:
    """读取并丢弃客户端的假 ClientKeyExchange + ChangeCipherSpec + Finished。"""
    try:
        saw_ccs = False
        async def _read_one():
            nonlocal saw_ccs
            ct, _ = await _read_tls_record(reader)
            if ct == 0x14:      # ChangeCipherSpec
                saw_ccs = True
                return False    # 继续读 Finished
            if ct == 0x16 and saw_ccs:  # Finished（在 CCS 之后的 Handshake 记录）
                return True
            return False

        await asyncio.wait_for(_drain_until(_read_one), timeout=5.0)
    except Exception:
        pass


async def _drain_until(coro_factory) -> None:
    """反复调用 coro_factory() 直到返回 True。"""
    while not await coro_factory():
        pass


async def server_read_hello_and_decide(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    password: str,
    cache: HandshakeCache,
) -> tuple[bool, bytes | None]:
    """
    读取 ClientHello，验证 session_id 中的认证 token。

    合法客户端 → 模拟完整 TLS 握手后返回 (True, client_random)。
    探测/非法  → 从缓存回放握手，由本函数负责关闭连接，返回 (False, None)。
    """
    try:
        _ct, hello_raw = await asyncio.wait_for(_read_tls_record(client_reader), timeout=8.0)
    except Exception as e:
        logger.debug("Failed to read ClientHello: %s", e)
        return False, None

    session_id    = extract_session_id(hello_raw)
    client_random = extract_client_random(hello_raw)

    if (session_id and len(session_id) == 32
            and client_random
            and verify_session_token(password, session_id)):
        logger.info("In-Hello auth OK")
        # 向合法客户端回放缓存的握手记录，使握手在旁观者眼中看起来完整
        ok = await cache.send_server_hello_done(client_writer)
        if not ok:
            logger.warning("Handshake cache empty, closing legitimate connection")
            client_writer.close()
            return False, None
        # 读取客户端的假 CKE+CCS+Finished
        await _read_client_tail(client_reader)
        # 发出假 ServerCCS+Finished，完成握手模拟
        try:
            client_writer.write(_fake_server_tail())
            await client_writer.drain()
        except Exception:
            return False, None
        return True, client_random

    # 探测连接：从缓存本地回放，不产生任何跨网络延迟
    logger.debug("Probe or unauthorized connection, replying from cache")
    await cache.serve_probe(client_reader, client_writer)
    return False, None
