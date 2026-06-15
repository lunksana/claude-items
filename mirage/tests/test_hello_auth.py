"""
握手认证 + 防重放 单元测试（安全核心）。

用 set_time_provider 注入可控时钟，确定性地测时间戳容差 / 重放窗口。

覆盖：
  - make_session_token ↔ verify_session_token round-trip
  - 错密码 / 过期时间戳 / 篡改字节 / 短 token 全拒
  - anti-fingerprint：同秒同密码两 token 不同（含 Poly1305 tag 不同）
  - TokenReplayCache：首次 True、重放 False
  - 临界 2T 重放窗口必须被命中（文档化的 3-bucket bug 修复回归）
  - ClientHello 解析（session_id / client_random / 畸形）
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import hello_auth as ha  # noqa: E402
from core.hello_auth import (  # noqa: E402
    make_session_token, verify_session_token, TokenReplayCache,
    set_time_provider, TIMESTAMP_TOLERANCE,
    extract_session_id, extract_client_random, _check_client_hello,
)

PW = "correct horse battery staple"


class _Clock:
    """可控时钟：t 可读写，单位秒。"""
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _ClockMixin:
    def setUp(self):
        self.clock = _Clock()
        set_time_provider(self.clock)

    def tearDown(self):
        import time
        set_time_provider(time.time)


class TestSessionToken(_ClockMixin, unittest.TestCase):
    def test_roundtrip(self):
        tok = make_session_token(PW)
        self.assertEqual(len(tok), 32)
        self.assertTrue(verify_session_token(PW, tok))

    def test_wrong_password_rejected(self):
        tok = make_session_token(PW)
        self.assertFalse(verify_session_token("wrong password", tok))

    def test_short_token_rejected(self):
        for n in (0, 8, 16, 31):
            self.assertFalse(verify_session_token(PW, b"\x00" * n))

    def test_expired_timestamp_rejected(self):
        tok = make_session_token(PW)
        # 时间前进超过容差 → 拒
        self.clock.t += TIMESTAMP_TOLERANCE + 5
        self.assertFalse(verify_session_token(PW, tok))

    def test_within_tolerance_accepted(self):
        tok = make_session_token(PW)
        self.clock.t += TIMESTAMP_TOLERANCE - 1
        self.assertTrue(verify_session_token(PW, tok))

    def test_future_timestamp_rejected(self):
        # token 在"未来"签发（时钟回拨），超容差也拒
        tok = make_session_token(PW)
        self.clock.t -= TIMESTAMP_TOLERANCE + 5
        self.assertFalse(verify_session_token(PW, tok))

    def test_tampered_tag_rejected(self):
        tok = bytearray(make_session_token(PW))
        tok[20] ^= 0xFF   # 翻 tag 里一个字节
        self.assertFalse(verify_session_token(PW, bytes(tok)))

    def test_tampered_hidden_ts_rejected(self):
        tok = bytearray(make_session_token(PW))
        tok[10] ^= 0xFF   # 翻掩码时间戳字节 → 解出错 ts → tag 不匹配 / 超时
        self.assertFalse(verify_session_token(PW, bytes(tok)))

    def test_tampered_prefix_rejected(self):
        tok = bytearray(make_session_token(PW))
        tok[0] ^= 0xFF    # 翻 random_prefix → one-time key 变 → tag 不匹配
        self.assertFalse(verify_session_token(PW, bytes(tok)))


class TestAntiFingerprint(_ClockMixin, unittest.TestCase):
    def test_same_second_tokens_differ(self):
        # 同秒同密码，两 token 必须完全不同（random_prefix 真随机）
        a = make_session_token(PW)
        b = make_session_token(PW)
        self.assertNotEqual(a, b)

    def test_same_second_tags_differ(self):
        # 关键 anti-fingerprint 属性：连 Poly1305 tag（末 16B）都不同，
        # 消除"同秒 ClientHello 末 16 字节一致"的可聚类指纹
        a = make_session_token(PW)
        b = make_session_token(PW)
        self.assertNotEqual(a[16:32], b[16:32])

    def test_both_valid(self):
        a = make_session_token(PW)
        b = make_session_token(PW)
        self.assertTrue(verify_session_token(PW, a))
        self.assertTrue(verify_session_token(PW, b))


class TestReplayCache(_ClockMixin, unittest.TestCase):
    def test_first_use_accepted(self):
        c = TokenReplayCache()
        tok = make_session_token(PW)
        self.assertTrue(c.check_and_mark(tok))

    def test_immediate_replay_rejected(self):
        c = TokenReplayCache()
        tok = make_session_token(PW)
        self.assertTrue(c.check_and_mark(tok))
        self.assertFalse(c.check_and_mark(tok))   # 重放

    def test_different_nonce_not_replay(self):
        c = TokenReplayCache()
        self.assertTrue(c.check_and_mark(make_session_token(PW)))
        self.assertTrue(c.check_and_mark(make_session_token(PW)))

    def test_replay_caught_across_full_2T_window(self):
        # 文档化的 3-bucket 修复回归：首次接受 → 2T 后重放仍须被命中。
        # 最坏点：token 在桶起点被接受，2T 整点重放。
        c = TokenReplayCache()
        # 对齐到桶边界起点
        base = (int(self.clock.t) // TIMESTAMP_TOLERANCE) * TIMESTAMP_TOLERANCE
        self.clock.t = float(base)
        tok = make_session_token(PW)
        self.assertTrue(c.check_and_mark(tok))
        # 前进到接近 2T（仍在 token 时间容差内的最大跨度）
        self.clock.t = float(base + 2 * TIMESTAMP_TOLERANCE - 1)
        self.assertFalse(c.check_and_mark(tok), "2T 窗口内重放必须被命中")

    def test_nonce_expires_after_window(self):
        # 超出保留窗口（3 桶）后，老 nonce 被清理 → 相同 nonce 不再算重放。
        # 这是可接受的：此时 token 本身已超时间容差，verify 会拒。
        c = TokenReplayCache()
        tok = make_session_token(PW)
        self.assertTrue(c.check_and_mark(tok))
        self.clock.t += 4 * TIMESTAMP_TOLERANCE   # 远超 3 桶
        self.assertTrue(c.check_and_mark(tok), "超窗口后桶已清，不再判重放")


class TestClientHelloParsing(unittest.TestCase):
    def _hello(self, session_id=b"", client_random=None):
        cr = client_random if client_random is not None else os.urandom(32)
        body = bytearray()
        body += b"\x16"            # content_type Handshake
        body += b"\x03\x03"        # legacy_record_version
        body += b"\x00\x00"        # length (占位)
        body += b"\x01"            # handshake_type ClientHello
        body += b"\x00\x00\x00"    # handshake_length (占位)
        body += b"\x03\x03"        # legacy_version
        body += cr                 # client_random (32B)
        body += bytes([len(session_id)])
        body += session_id
        return bytes(body)

    def test_extract_client_random(self):
        cr = os.urandom(32)
        self.assertEqual(extract_client_random(self._hello(client_random=cr)), cr)

    def test_extract_session_id(self):
        sid = os.urandom(32)
        self.assertEqual(extract_session_id(self._hello(session_id=sid)), sid)

    def test_empty_session_id(self):
        self.assertEqual(extract_session_id(self._hello(session_id=b"")), b"")

    def test_non_clienthello_returns_none(self):
        bad = bytearray(self._hello(session_id=b"x" * 4))
        bad[0] = 0x17   # 不是 Handshake
        self.assertIsNone(extract_session_id(bytes(bad)))
        self.assertIsNone(extract_client_random(bytes(bad)))

    def test_truncated_returns_none(self):
        full = self._hello(session_id=os.urandom(16))
        self.assertIsNone(extract_session_id(full[:30]))   # 不够 44B
        # session_id_len 宣称 16 但被截断
        self.assertIsNone(extract_session_id(full[:50]))

    def test_check_client_hello(self):
        self.assertTrue(_check_client_hello(self._hello(session_id=b"abc")))
        self.assertFalse(_check_client_hello(b"\x16\x03\x03"))   # 太短


if __name__ == "__main__":
    unittest.main(verbosity=2)
