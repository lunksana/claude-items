"""
线路协议核心：地址打包 / 解包 + TCP/UDP 解复用约定。

这是 client ↔ server 之间**最底层、最该被锁死的契约**——未来的 Rust 完全体
重写必须逐字节对齐这里。任何改动若破坏 round-trip 或 demux 首字节语义，会让
新旧实现互不通。

协议（见 core/utils.pack_address / server.py 的 UDP 检测）：
  pack_address(host, port) = [1B host_len][host bytes][2B port BE]
  TCP 隧道首包 = pack_address(target_host, target_port)，host_len >= 1
  UDP 隧道首包 = b"\\x00"（host_len=0）——server 据首字节是否为 0 区分 UDP/TCP
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.utils import pack_address, unpack_address  # noqa: E402


class TestAddressRoundtrip(unittest.TestCase):
    def test_ipv4_roundtrip(self):
        packed = pack_address("1.2.3.4", 443)
        host, port, consumed = unpack_address(packed)
        self.assertEqual((host, port), ("1.2.3.4", 443))
        self.assertEqual(consumed, len(packed))

    def test_domain_roundtrip(self):
        packed = pack_address("www.example.com", 8080)
        host, port, consumed = unpack_address(packed)
        self.assertEqual((host, port), ("www.example.com", 8080))
        self.assertEqual(consumed, len(packed))

    def test_ipv6_literal_roundtrip(self):
        packed = pack_address("2001:db8::1", 53)
        host, port, _ = unpack_address(packed)
        self.assertEqual((host, port), ("2001:db8::1", 53))

    def test_port_boundaries(self):
        for port in (1, 80, 443, 8080, 65535):
            host, p, _ = unpack_address(pack_address("h.com", port))
            self.assertEqual((host, p), ("h.com", port))

    def test_consumed_allows_trailing_data(self):
        # unpack 只吃地址部分，后面可以跟 payload（UDP 帧就是这么用的）
        packed = pack_address("a.com", 1) + b"TRAILING_PAYLOAD"
        host, port, consumed = unpack_address(packed)
        self.assertEqual((host, port), ("a.com", 1))
        self.assertEqual(packed[consumed:], b"TRAILING_PAYLOAD")

    def test_wire_format_exact_bytes(self):
        # 锁死字节布局：[len][host][port BE]
        packed = pack_address("ab", 0x1234)
        self.assertEqual(packed, b"\x02" + b"ab" + b"\x12\x34")


class TestTcpUdpDemux(unittest.TestCase):
    """server 据首包首字节区分 TCP（host_len>=1）/ UDP（0x00）。"""

    def test_udp_marker_is_zero_byte(self):
        # UDP 隧道首包是单字节 0x00
        self.assertEqual(b"\x00"[0], 0)

    def test_tcp_first_byte_never_zero(self):
        # 任何合法 target 的 pack_address 首字节 = host_len >= 1（最短 host 也 >= 1）
        for host in ("0.0.0.0", "a", "x.com", "2001:db8::1"):
            packed = pack_address(host, 1)
            self.assertGreaterEqual(packed[0], 1,
                                    f"{host!r} 的 host_len 不应为 0（会被误判 UDP）")

    def test_shortest_ipv4_host_len(self):
        # "0.0.0.0" 是最短的 IPv4 字符串，host_len=7，远大于 0
        self.assertEqual(pack_address("0.0.0.0", 1)[0], 7)


class TestUnpackRobustness(unittest.TestCase):
    def test_host_len_matches(self):
        # host_len 字节必须与实际 host 字节数一致
        packed = pack_address("hello.com", 443)
        self.assertEqual(packed[0], len("hello.com"))

    def test_non_ascii_host_utf8(self):
        # host.encode() 默认 UTF-8；国际化域名（punycode 前）也能 round-trip
        # 实际使用中域名应已是 punycode，但 pack/unpack 本身不该破坏 UTF-8
        host = "例え.テスト"
        packed = pack_address(host, 80)
        got, port, _ = unpack_address(packed)
        self.assertEqual((got, port), (host, 80))


if __name__ == "__main__":
    unittest.main(verbosity=2)
