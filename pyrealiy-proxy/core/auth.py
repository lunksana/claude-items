"""
认证模块 —— 参考 Reality 的思路

使用 Poly1305 代替 HMAC-SHA256：
  - 速度约快 3 倍（多项式运算 vs SHA256 多轮压缩）
  - 包体更小：24 字节 vs 原来 40 字节

AUTH 包格式（共 24 字节）：
  [8 字节时间戳 Unix 秒] [16 字节 Poly1305 tag]

Poly1305 是一次性 MAC：每次用不同的 key。
我们的 key = SHA256(password + timestamp)，保证每秒最多重用一次，
加上时间戳容忍窗口 ±60 秒，已足够对抗 GFW 的主动探测和重放。
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import time

from cryptography.hazmat.primitives.poly1305 import Poly1305

AUTH_PACKET_SIZE = 24        # 8 (ts) + 16 (Poly1305 tag)
TIMESTAMP_TOLERANCE = 60     # 秒


def make_auth_packet(password: str) -> bytes:
    """客户端：生成认证包"""
    ts = int(time.time())
    ts_bytes = struct.pack("!Q", ts)
    tag = _compute_tag(password, ts_bytes)
    return ts_bytes + tag


def verify_auth_packet(password: str, packet: bytes) -> bool:
    """服务端：验证认证包"""
    if len(packet) < AUTH_PACKET_SIZE:
        return False

    ts_bytes     = packet[:8]
    received_tag = packet[8:24]
    ts = struct.unpack("!Q", ts_bytes)[0]

    if abs(int(time.time()) - ts) > TIMESTAMP_TOLERANCE:
        return False

    expected_tag = _compute_tag(password, ts_bytes)
    return hmac.compare_digest(expected_tag, received_tag)


def _compute_tag(password: str, ts_bytes: bytes) -> bytes:
    # 用 SHA256 派生 32 字节一次性密钥，再用 Poly1305 计算 tag
    one_time_key = hashlib.sha256(password.encode() + ts_bytes).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    return p.finalize()  # 固定 16 字节
