"""
EncryptedTunnel 加密往返测试。

用 socket.socketpair() 建本地连接对（无外网），两端各包一个 EncryptedTunnel，
做握手后 send/recv，验证真实 ChaCha20-Poly1305 往返：
  - 单向 / 双向 round-trip
  - 大 payload 跨多 record 重组（> 16KB record 上限）
  - 方向独立密钥（c2s ≠ s2c）
  - 错密码 → AEAD 解密失败
  - close_notify → recv 抛 EOFError
  - 多帧 nonce 顺序正确
  - _derive_master / _expand 确定性

涉及真 socket + asyncio，属轻量集成；全本地 loopback，确定性。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tunnel import EncryptedTunnel, _derive_master, _expand  # noqa: E402

PW = "tunnel-test-password"


async def _make_pair(password=PW, client_random=None, peer_password=None):
    """建一对握手完成的 EncryptedTunnel（initiator, responder）。"""
    cr = client_random if client_random is not None else os.urandom(32)
    s1, s2 = socket.socketpair()
    for s in (s1, s2):
        s.setblocking(False)
    r1, w1 = await asyncio.open_connection(sock=s1)
    r2, w2 = await asyncio.open_connection(sock=s2)
    init = EncryptedTunnel(r1, w1, password)
    resp = EncryptedTunnel(r2, w2, peer_password or password)
    await init.do_handshake_as_initiator(cr)
    await resp.do_handshake_as_responder(cr)
    return init, resp, (w1, w2)


async def _close(writers):
    for w in writers:
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass


class TestTunnelRoundtrip(unittest.TestCase):
    def test_single_message(self):
        async def go():
            init, resp, ws = await _make_pair()
            try:
                await init.send(b"hello tunnel")
                self.assertEqual(await resp.recv(), b"hello tunnel")
            finally:
                await _close(ws)
        asyncio.run(go())

    def test_bidirectional(self):
        async def go():
            init, resp, ws = await _make_pair()
            try:
                await init.send(b"c2s msg")
                self.assertEqual(await resp.recv(), b"c2s msg")
                await resp.send(b"s2c reply")
                self.assertEqual(await init.recv(), b"s2c reply")
            finally:
                await _close(ws)
        asyncio.run(go())

    def test_multiple_frames_in_order(self):
        async def go():
            init, resp, ws = await _make_pair()
            try:
                msgs = [f"msg-{i}".encode() for i in range(10)]
                for m in msgs:
                    await init.send(m)
                got = [await resp.recv() for _ in msgs]
                self.assertEqual(got, msgs)
            finally:
                await _close(ws)
        asyncio.run(go())

    def test_large_payload_reassembles(self):
        # > 16KB → send 会切多条 record；recv 逐条返回，拼接后应等于原文
        async def go():
            init, resp, ws = await _make_pair()
            try:
                payload = os.urandom(100_000)
                await init.send(payload)
                got = b""
                while len(got) < len(payload):
                    got += await resp.recv()
                self.assertEqual(got, payload)
            finally:
                await _close(ws)
        asyncio.run(go())

    def test_binary_safe(self):
        async def go():
            init, resp, ws = await _make_pair()
            try:
                data = bytes(range(256)) * 4
                await init.send(data)
                got = b""
                while len(got) < len(data):
                    got += await resp.recv()
                self.assertEqual(got, data)
            finally:
                await _close(ws)
        asyncio.run(go())


class TestTunnelSecurity(unittest.TestCase):
    def test_wrong_password_fails_decrypt(self):
        # 两端密码不同 → 密钥不同 → AEAD 解密抛
        async def go():
            init, resp, ws = await _make_pair(password=PW, peer_password="different")
            try:
                await init.send(b"secret")
                with self.assertRaises(Exception):
                    await resp.recv()
            finally:
                await _close(ws)
        asyncio.run(go())

    def test_directional_keys_differ(self):
        # c2s 与 s2c 用不同 info 派生 → 同一 master 下两方向密钥不同
        master = _derive_master(PW, b"\x00" * 32)
        self.assertNotEqual(_expand(master, b"c2s"), _expand(master, b"s2c"))

    def test_close_notify_raises_eof(self):
        async def go():
            init, resp, ws = await _make_pair()
            try:
                await init.send_close_notify()
                with self.assertRaises(EOFError):
                    await resp.recv()
            finally:
                await _close(ws)
        asyncio.run(go())


class TestKeyDerivation(unittest.TestCase):
    def test_derive_master_deterministic(self):
        a = _derive_master(PW, b"salt" * 8)
        b = _derive_master(PW, b"salt" * 8)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_derive_master_salt_sensitive(self):
        a = _derive_master(PW, b"\x00" * 32)
        b = _derive_master(PW, b"\x01" * 32)
        self.assertNotEqual(a, b)

    def test_derive_master_password_sensitive(self):
        salt = b"\x00" * 32
        self.assertNotEqual(_derive_master("pw1", salt), _derive_master("pw2", salt))

    def test_expand_deterministic_and_distinct(self):
        master = _derive_master(PW, b"\x00" * 32)
        self.assertEqual(_expand(master, b"c2s"), _expand(master, b"c2s"))
        self.assertEqual(len(_expand(master, b"c2s")), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
