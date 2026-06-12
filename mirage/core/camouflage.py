"""
TLS 伪装模块

服务端收到 TCP 连接后先读取 ClientHello，检查 legacy_session_id：

  ┌─ 含有效认证 token ──► 服务端回放缓存的 TLS 1.3 握手记录（ServerHello + CCS + 加密记录群），
  │                        读取客户端的 CCS+Finished，返回 (True, client_random)。
  │                        握手完成后双方直接进入加密应用数据模式。
  │
  └─ 普通/无效 token  ──► 从本地缓存回放握手记录，探测器收到真实 apple.com TLS 1.3 记录，
                           无法在不掌握会话密钥的情况下继续，返回 (False, None)。

TLS 1.3 握手序列（双方视角）：
  S→C: ServerHello(0x16) + CCS(0x14) + EncryptedExtensions + Certificate +
        CertificateVerify + Finished（后四者均为 0x17 加密记录）
  C→S: CCS(0x14) + Finished(0x17)
  C↔S: Application Data(0x17)，即代理流量
"""

from __future__ import annotations

import asyncio

from .hello_auth import (
    extract_session_id, extract_client_random,
    verify_session_token, TokenReplayCache,
)
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


async def _read_client_tail(reader: asyncio.StreamReader) -> None:
    """
    读取并丢弃客户端的 TLS 1.3 client flight：
      ChangeCipherSpec(0x14) + Finished(0x17, Encrypted Application Data)
    """
    async def _drain():
        saw_ccs = False
        while True:
            ct, _ = await _read_tls_record(reader)
            if ct == 0x14:
                saw_ccs = True
            elif ct == 0x17 and saw_ccs:
                return  # client Finished 已读完
    try:
        await asyncio.wait_for(_drain(), timeout=5.0)
    except Exception:
        pass


async def server_read_hello_and_decide(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    password: str,
    cache: HandshakeCache,
    replay_cache: TokenReplayCache,
) -> tuple[bool, bytes | None]:
    """
    读取 ClientHello，验证 session_id 中的认证 token。

    合法客户端 → 模拟完整 TLS 1.3 握手后返回 (True, client_random)。
    探测/非法/重放 → 从缓存回放握手，由本函数负责关闭连接，返回 (False, None)。
    """
    try:
        _ct, hello_raw = await asyncio.wait_for(_read_tls_record(client_reader), timeout=8.0)
    except Exception as e:
        logger.debug("Failed to read ClientHello: %s", e)
        try:
            client_writer.close()
        except Exception:
            pass
        return False, None

    session_id    = extract_session_id(hello_raw)
    client_random = extract_client_random(hello_raw)

    if (session_id and len(session_id) == 32
            and client_random
            and verify_session_token(password, session_id)
            and replay_cache.check_and_mark(session_id)):
        logger.info("In-Hello auth OK")
        # 把客户端的 session_id（即我们的 token）传给 send_server_hello_done，
        # 让 ServerHello.session_id_echo 与之严格对应（TLS 1.3 spec MUST）
        ok = await cache.send_server_hello_done(client_writer, session_id)
        if not ok:
            logger.warning("Handshake cache empty, closing legitimate connection")
            client_writer.close()
            return False, None
        # 读取客户端的 CCS + Finished，完成握手模拟
        await _read_client_tail(client_reader)
        # TLS 1.3：server 在初始 flight 中已发完所有握手消息，无需再发 ServerFinished
        return True, client_random

    logger.debug("Probe or unauthorized connection, replying from cache")
    # 探测路径也 echo 探测端的 session_id（如果 ClientHello 里有的话）—— 真实
    # TLS server 必须 echo，伪装路径也照做避免行为不一致被 GFW 识别
    await cache.serve_probe(client_reader, client_writer, probe_session_id=session_id)
    return False, None
