"""
ClientHello 内嵌认证模块

TLS ClientHello 中有一个 legacy_session_id 字段（TLS 1.3 规范要求 32 字节随机数）。
我们把它替换成 32 字节认证 token，对旁观者来说与随机数无法区分。

Token 布局（32 字节）：
  [8 字节随机前缀]  ← 让 token 看起来更随机
  [8 字节时间戳]
  [16 字节 Poly1305 tag]

服务端解析 ClientHello，取出 session_id，验证 Poly1305 tag。
验证通过 → 直接进代理模式，不转发给伪装站点（零额外延迟）。
验证失败 → 转发给伪装站点，GFW 看到正常响应。
"""


from __future__ import annotations

import hashlib

import hmac

import os

import struct

import time


from cryptography.hazmat.primitives.poly1305 import Poly1305

TIMESTAMP_TOLERANCE = 60  # 秒


# ── Token 生成 / 验证 ─────────────────────────────────────────────────────────

def make_session_token(password: str) -> bytes:
    """生成 32 字节 session token，嵌入 ClientHello 的 legacy_session_id"""
    random_prefix = os.urandom(8)
    ts = int(time.time())
    ts_bytes = struct.pack("!Q", ts)
    one_time_key = hashlib.sha256(password.encode() + ts_bytes).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    tag = p.finalize()  # 16 字节
    return random_prefix + ts_bytes + tag


def verify_session_token(password: str, token: bytes) -> bool:
    """验证 32 字节 session token"""
    if len(token) < 32:
        return False
    ts_bytes = token[8:16]
    received_tag = token[16:32]
    ts = struct.unpack("!Q", ts_bytes)[0]

    if abs(int(time.time()) - ts) > TIMESTAMP_TOLERANCE:
        return False

    one_time_key = hashlib.sha256(password.encode() + ts_bytes).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    expected_tag = p.finalize()
    return hmac.compare_digest(expected_tag, received_tag)


# ── ClientHello 解析 ──────────────────────────────────────────────────────────

def extract_session_id(record_bytes: bytes) -> bytes | None:
    """
    从原始 TLS 记录字节中提取 legacy_session_id。

    TLS 记录结构：
      [0]    content_type = 0x16 (Handshake)
      [1:3]  legacy_record_version
      [3:5]  length
      [5]    handshake_type = 0x01 (ClientHello)
      [6:9]  handshake_length (3 字节)
      [9:11] legacy_version
      [11:43] client_random (32 字节)
      [43]   session_id_length
      [44:]  session_id
    """
    try:
        if len(record_bytes) < 44:
            return None
        if record_bytes[0] != 0x16:  # 必须是 Handshake
            return None
        if record_bytes[5] != 0x01:  # 必须是 ClientHello
            return None

        session_id_len = record_bytes[43]
        if session_id_len == 0:
            return b""
        end = 44 + session_id_len
        if end > len(record_bytes):
            return None
        return record_bytes[44:end]
    except IndexError:
        return None
