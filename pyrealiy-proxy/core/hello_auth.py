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

# ── 时钟源（默认系统时钟；TimeSync.start() 后会注入校正后的时间）─────────────
#   client/server.py 启动期调 set_time_provider(TimeSync.corrected_time) 后，
#   下面所有时间相关计算（token 时间戳、replay cache 桶）都走带 offset 的时间，
#   避免 VPS 时钟漂移 > TIMESTAMP_TOLERANCE 导致全部 token 被误判超时。
from typing import Callable as _Callable

_time_provider: _Callable[[], float] = time.time


def set_time_provider(fn: _Callable[[], float]) -> None:
    """注入时间源。fn() 返回 Unix UTC 秒（小数）"""
    global _time_provider
    _time_provider = fn


def _now() -> int:
    """整数 Unix 秒。所有 token 流程走这个，不直接调 time.time()"""
    return int(_time_provider())


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

    ──── 关于桶数为什么必须是 3，不能是 2 ────
      同一 token 被合法接受的服务端时间窗口是 [ts-T, ts+T]，所以
        首次接受 → 重放 的最大时间跨度 = 2T = 120s

      老实现只留 {current, current-1} 两桶。临界场景：
        客户端 ts=60，首次在服务端 t=59 被接受 → 存入 bucket 0
        服务端 t=120 时攻击者重放，bucket=2，|120-60|=60 通过容差校验
        清理逻辑删除 bucket < 1 → bucket 0 被删 → 查 (2,1) 找不到 → 重放放行

      根本原因：bucket 宽度 W = T，但需要留存的最长年龄 = 2T。最坏情况
      （当前桶刚刚开始 elapsed=0）下两桶覆盖的最老年龄 = W = T < 2T，存在
      120s 整点处的盲区。

      正确做法：留存 ⌈2T/W⌉ + 1 = 3 桶 {current, current-1, current-2}。
      在 elapsed=0 的最坏点，bucket-2 内的项年龄区间是 [W, 2W) = [T, 2T)，
      恰好覆盖到 2T 整点，临界重放被命中。

    实现：
      使用时间桶（宽度 = TIMESTAMP_TOLERANCE）分组存储 nonce。
      保留当前桶、上一桶、上上桶共 3 个，覆盖最差 2T 重放窗口。
      asyncio 单线程模型下无需加锁。

    内存开销：
      每个 token 仅存 8 字节 nonce；即使每秒 1000 次连接，3 × 60s 滚动窗口
      内也仅需 ~1.4 MB。
    """

    # 必须 ≥ ⌈2 × TIMESTAMP_TOLERANCE / 桶宽⌉ + 1；
    # 桶宽 == TIMESTAMP_TOLERANCE，所以最小是 3。详见类 docstring 的临界分析。
    _BUCKETS_KEPT = 3

    def __init__(self) -> None:
        self._buckets: dict[int, set[bytes]] = {}

    def _bucket(self) -> int:
        return _now() // TIMESTAMP_TOLERANCE

    def check_and_mark(self, token: bytes) -> bool:
        """
        检查 token 是否为首次使用。

        返回 True  → 合法（首次出现），同时将其 nonce 标记为已使用。
        返回 False → 重放攻击，调用方应走伪装路径而非代理路径。
        """
        nonce  = token[:8]
        bucket = self._bucket()
        keep_from = bucket - (self._BUCKETS_KEPT - 1)

        # 清理过期桶（保留最近 _BUCKETS_KEPT 个）
        for stale in [b for b in self._buckets if b < keep_from]:
            del self._buckets[stale]

        # 在保留窗口内的所有桶查找重复
        for b in range(bucket, keep_from - 1, -1):
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


def _poly1305_tag(password_bytes: bytes, ts_bytes: bytes, random_prefix: bytes) -> bytes:
    """
    One-time key 派生：SHA256(password || ts || random_prefix)。

    把 random_prefix 混进 key（**不是 message**）：
      - 每个 token 的 one-time key 都不同 → 即使同秒签同一条 ts_bytes，tag 也不同
        （消除"同秒 ClientHello 末 16 字节完全一致"这种可被 GFW 聚类的指纹）
      - 仍然满足 Poly1305 的"one-time key per message"安全要求
        （注意：random_prefix 混进 message 会导致同 key 签不同 message → 密钥恢复）
    """
    one_time_key = hashlib.sha256(password_bytes + ts_bytes + random_prefix).digest()
    p = Poly1305(one_time_key)
    p.update(ts_bytes)
    return p.finalize()


def make_session_token(password: str) -> bytes:
    """生成 32 字节 session token，嵌入 ClientHello 的 legacy_session_id"""
    random_prefix = os.urandom(8)
    ts = _now()   # 经 TimeSync 校正的 Unix 秒
    ts_bytes = struct.pack("!Q", ts)
    # 掩码时间戳：XOR 后字节分布均匀，统计分析无法从中提取时间信息
    mask = _ts_mask(password, random_prefix)
    hidden_ts = bytes(a ^ b for a, b in zip(ts_bytes, mask))
    tag = _poly1305_tag(password.encode(), ts_bytes, random_prefix)
    return random_prefix + hidden_ts + tag


def verify_session_token(password: str, token: bytes) -> bool:
    """
    验证 32 字节 session token。

    Token 结构：[8B random_prefix][8B 掩码时间戳][16B Poly1305 tag]
    防重放由 TokenReplayCache 兜住（按 random_prefix 去重）。
    """
    if len(token) < 32:
        return False
    random_prefix = token[0:8]
    hidden_ts    = token[8:16]
    received_tag = token[16:32]

    mask    = _ts_mask(password, random_prefix)
    ts_bytes = bytes(a ^ b for a, b in zip(hidden_ts, mask))
    ts = struct.unpack("!Q", ts_bytes)[0]

    if abs(_now() - ts) > TIMESTAMP_TOLERANCE:
        return False

    expected_tag = _poly1305_tag(password.encode(), ts_bytes, random_prefix)
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
