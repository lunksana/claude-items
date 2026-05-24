"""
ClientHello 内嵌认证模块

TLS ClientHello 中有一个 legacy_session_id 字段（TLS 1.3 规范要求 32 字节随机数）。
我们把它替换成 32 字节认证 token，对旁观者来说与随机数无法区分。

Token 布局（32 字节）：
  [8 字节随机前缀]
  [8 字节时间戳 ^ HMAC-SHA256(sha256(password), random_prefix)[:8]]  ← 掩码后全字节均匀分布
  [16 字节 Poly1305 tag]

掩码目的：原始 Unix 时间戳高位字节长期为 0x00，统计分析可轻易识别。
掩码后 8 字节对外观察者与真随机数无法区分，但服务端持有密码可还原时间戳并验证。

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


# ── Token 重放缓存 ─────────────────────────────────────────────────────────────

class TokenReplayCache:
    """
    防重放 nonce 缓存。

    问题背景：
      ClientHello 明文传输，GFW 可在 TIMESTAMP_TOLERANCE（60s）窗口内
      重放捕获到的合法 ClientHello，通过观察服务端行为差异（代理模式无限期挂起
      vs 伪装模式快速断开）以 100% 准确率识别代理特征。

    解决方案：
      服务端维护已见 nonce（token[0:8]，8 字节真随机）的短生命周期缓存。
      重复出现的 nonce 无论 Poly1305 验证是否通过，均视为重放并走伪装路径。

    实现：
      使用时间桶（宽度 = TIMESTAMP_TOLERANCE）分组存储 nonce。
      保留当前桶和上一桶，覆盖跨桶边界生成的 token。
      asyncio 单线程模型下无需加锁。

    内存开销：
      每个 token 仅存 8 字节 nonce；即使每秒 1000 次连接，60s 内也仅需 ~480 KB。
    """

    def __init__(self) -> None:
        self._buckets: dict[int, set[bytes]] = {}

    def _bucket(self) -> int:
        return int(time.time()) // TIMESTAMP_TOLERANCE

    def check_and_mark(self, token: bytes) -> bool:
        """
        检查 token 是否为首次使用。

        返回 True  → 合法（首次出现），同时将其 nonce 标记为已使用。
        返回 False → 重放攻击，调用方应走伪装路径而非代理路径。
        """
        nonce  = token[:8]
        bucket = self._bucket()

        # 清理过期桶（仅保留当前和上一桶）
        for stale in [b for b in self._buckets if b < bucket - 1]:
            del self._buckets[stale]

        # 在当前桶和上一桶中查找重复
        for b in (bucket, bucket - 1):
            if nonce in self._buckets.get(b, ()):
                return False

        # 首次出现：写入当前桶
        self._buckets.setdefault(bucket, set()).add(nonce)
        return True


# ── Token 生成 / 验证 ─────────────────────────────────────────────────────────

def _ts_mask(password: str, random_prefix: bytes) -> bytes:
    """派生 8 字节掩码，用于加密/解密 token 中的时间戳字节"""
    pw_key = hashlib.sha256(password.encode()).digest()
    return hmac.new(pw_key, random_prefix, hashlib.sha256).digest()[:8]


def make_session_token(password: str) -> bytes:
    """生成 32 字节 session token，嵌入 ClientHello 的 legacy_session_id"""
    random_prefix = os.urandom(8)
    ts = int(time.time())
    ts_bytes = struct.pack("!Q", ts)
    # 掩码时间戳：XOR 后字节分布均匀，统计分析无法从中提取时间信息
    mask = _ts_mask(password, random_prefix)
    hidden_ts = bytes(a ^ b for a, b in zip(ts_bytes, mask))
    one_time_key = hashlib.sha256(password.encode() + ts_bytes).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    tag = p.finalize()  # 16 字节
    return random_prefix + hidden_ts + tag


def verify_session_token(password: str, token: bytes) -> bool:
    """验证 32 字节 session token"""
    if len(token) < 32:
        return False
    random_prefix = token[0:8]
    hidden_ts    = token[8:16]
    received_tag = token[16:32]

    # 还原时间戳
    mask    = _ts_mask(password, random_prefix)
    ts_bytes = bytes(a ^ b for a, b in zip(hidden_ts, mask))
    ts = struct.unpack("!Q", ts_bytes)[0]

    if abs(int(time.time()) - ts) > TIMESTAMP_TOLERANCE:
        return False

    one_time_key = hashlib.sha256(password.encode() + ts_bytes).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    expected_tag = p.finalize()
    return hmac.compare_digest(expected_tag, received_tag)


# ── ClientHello 解析 ──────────────────────────────────────────────────────────
#
# TLS 记录结构（ClientHello）：
#   [0]     content_type = 0x16 (Handshake)
#   [1:3]   legacy_record_version
#   [3:5]   length
#   [5]     handshake_type = 0x01 (ClientHello)
#   [6:9]   handshake_length (3 字节)
#   [9:11]  legacy_version
#   [11:43] client_random (32 字节)
#   [43]    session_id_length
#   [44:]   session_id

def _check_client_hello(record_bytes: bytes) -> bool:
    return (len(record_bytes) >= 44
            and record_bytes[0] == 0x16
            and record_bytes[5] == 0x01)


def extract_session_id(record_bytes: bytes) -> bytes | None:
    """从原始 TLS 记录字节中提取 legacy_session_id"""
    try:
        if not _check_client_hello(record_bytes):
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


def extract_client_random(record_bytes: bytes) -> bytes | None:
    """从原始 TLS 记录字节中提取 client_random（32 字节）"""
    try:
        if not _check_client_hello(record_bytes):
            return None
        return record_bytes[11:43]
    except IndexError:
        return None
