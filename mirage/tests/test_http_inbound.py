"""
HTTP / Mixed inbound 解析测试。

入站解析处理**不可信的客户端输入**——畸形请求行 / 超大 header / 错误 CONNECT
目标等都不能让进程崩或挂，必须回对应 HTTP 状态码并关连接。这些错误路径在调到
后端 _dispatch 之前就返回，可独立测试（无需起真隧道）。

用真 asyncio.StreamReader 喂字节 + FakeWriter 捕获响应。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.http_inbound import (  # noqa: E402
    _parse_authority, handle_http_connection, handle_mixed_connection,
)


class _FakeTransport:
    def get_extra_info(self, _key, default=None):
        return default


class _FakeWriter:
    """捕获所有 write 的最小 StreamWriter 替身（safe_close 的调用都 try/except 包着）。"""
    def __init__(self):
        self.buf = bytearray()
        self._closing = False
        self.transport = _FakeTransport()

    def write(self, data):
        self.buf.extend(data)

    def write_eof(self):
        pass

    async def drain(self):
        pass

    def close(self):
        self._closing = True

    def is_closing(self):
        return self._closing

    async def wait_closed(self):
        pass

    def get_extra_info(self, _key, default=None):
        return default

    @property
    def text(self):
        return bytes(self.buf).decode("latin-1", errors="replace")


def _make_reader(data: bytes) -> asyncio.StreamReader:
    # 必须在 running loop 内构造（3.10+ StreamReader 构造期取 current loop）
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def _run_http(data: bytes) -> _FakeWriter:
    w = _FakeWriter()

    async def go():
        # outbounds/router/udp_cfg 不会被错误路径用到，传占位
        await handle_http_connection(_make_reader(data), w, {}, None, {})

    asyncio.run(go())
    return w


def _run_mixed(data: bytes) -> _FakeWriter:
    w = _FakeWriter()

    async def go():
        await handle_mixed_connection(_make_reader(data), w, {}, None, {})

    asyncio.run(go())
    return w


class TestParseAuthority(unittest.TestCase):
    def test_ipv4_host_port(self):
        self.assertEqual(_parse_authority("1.2.3.4:443"), ("1.2.3.4", 443))

    def test_domain_host_port(self):
        self.assertEqual(_parse_authority("example.com:8080"), ("example.com", 8080))

    def test_ipv6_bracketed(self):
        self.assertEqual(_parse_authority("[::1]:443"), ("::1", 443))

    def test_ipv6_full(self):
        self.assertEqual(_parse_authority("[2001:db8::1]:9000"), ("2001:db8::1", 9000))

    def test_missing_port_rejected(self):
        self.assertEqual(_parse_authority("example.com"), (None, 0))

    def test_non_numeric_port_rejected(self):
        self.assertEqual(_parse_authority("example.com:abc"), (None, 0))

    def test_port_out_of_range_rejected(self):
        self.assertEqual(_parse_authority("example.com:0"), (None, 0))
        self.assertEqual(_parse_authority("example.com:70000"), (None, 0))

    def test_ipv6_missing_colon_after_bracket(self):
        self.assertEqual(_parse_authority("[::1]443"), (None, 0))

    def test_empty_host_rejected(self):
        self.assertEqual(_parse_authority(":443"), (None, 0))


class TestHttpErrorPaths(unittest.TestCase):
    """畸形请求 → 对应状态码，不崩、不进 dispatch。"""

    def test_bad_http_version(self):
        w = _run_http(b"GET http://x.com/ HTTP/2.0\r\n\r\n")
        self.assertIn("505", w.text)

    def test_malformed_request_line(self):
        w = _run_http(b"GARBAGE\r\n\r\n")
        self.assertIn("400", w.text)

    def test_connect_bad_authority(self):
        w = _run_http(b"CONNECT not-a-valid-target HTTP/1.1\r\n\r\n")
        self.assertIn("400", w.text)

    def test_forward_requires_absolute_url(self):
        # 非 CONNECT 且 target 不是绝对 URL（相对路径）→ 400
        w = _run_http(b"GET /relative/path HTTP/1.1\r\nHost: x.com\r\n\r\n")
        self.assertIn("400", w.text)

    def test_oversized_header_line(self):
        big = b"GET http://x.com/ HTTP/1.1\r\nX-Big: " + b"A" * 9000 + b"\r\n\r\n"
        w = _run_http(big)
        self.assertIn("431", w.text)

    def test_too_many_headers(self):
        many = b"GET http://x.com/ HTTP/1.1\r\n"
        many += b"".join(b"H%d: v\r\n" % i for i in range(120))
        many += b"\r\n"
        w = _run_http(many)
        self.assertIn("431", w.text)

    def test_malformed_header_no_colon(self):
        w = _run_http(b"GET http://x.com/ HTTP/1.1\r\nNoColonHere\r\n\r\n")
        self.assertIn("400", w.text)

    def test_eof_before_request_closes(self):
        # 空输入 → 不崩，直接关
        w = _run_http(b"")
        self.assertTrue(w.is_closing())


class TestMixedDemux(unittest.TestCase):
    """mixed 首字节分发的自包含分支（不进 client 后端）。"""

    def test_tls_first_byte_returns_400_hint(self):
        # 0x16 = TLS ClientHello → 提示用错 scheme
        w = _run_mixed(b"\x16\x03\x01\x00\x05hello")
        self.assertIn("400", w.text)
        self.assertIn("TLS", w.text)

    def test_unknown_first_byte_closes(self):
        w = _run_mixed(b"\xff\xff\xff")
        self.assertTrue(w.is_closing())
        # 未知字节不回任何 HTTP 响应
        self.assertEqual(len(w.buf), 0)

    def test_empty_closes(self):
        w = _run_mixed(b"")
        self.assertTrue(w.is_closing())


if __name__ == "__main__":
    unittest.main(verbosity=2)
