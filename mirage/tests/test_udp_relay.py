"""
UDP relay 编解码 / 工具函数 单元测试。

覆盖：
  - pack_udp_frame ↔ FrameReader：单帧、多帧串接、分片喂入、跨帧 buffer
  - build_socks5_udp_header ↔ parse_socks5_udp_header：v4/v6/domain 三种 ATYP
  - 畸形输入安全（不 crash，返 None / 不 yield）
  - 工具函数：_is_ip_literal / _unmap_v4

UDPRelay 类 + handle_udp_tunnel 涉及真 socket，留给集成测试，不在这里。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.udp_relay import (  # noqa: E402
    pack_udp_frame, FrameReader,
    parse_socks5_udp_header, build_socks5_udp_header,
    _is_ip_literal, _unmap_v4,
)


class TestTunnelFrame(unittest.TestCase):
    """pack_udp_frame ↔ FrameReader 对称。"""

    def test_single_frame_roundtrip(self):
        frame = pack_udp_frame("1.2.3.4", 53, b"\x12\x34hello")
        r = FrameReader()
        r.feed(frame)
        out = list(r.frames())
        self.assertEqual(out, [("1.2.3.4", 53, b"\x12\x34hello")])

    def test_multiple_frames_concatenated(self):
        a = pack_udp_frame("8.8.8.8", 53, b"A")
        b = pack_udp_frame("example.com", 443, b"BB")
        r = FrameReader()
        r.feed(a + b)
        out = list(r.frames())
        self.assertEqual(out, [
            ("8.8.8.8", 53, b"A"),
            ("example.com", 443, b"BB"),
        ])

    def test_fragmented_feed_does_not_lose_frame(self):
        # 模拟 tunnel.recv 切到帧中间：每次喂 1 字节也得能拼出完整帧
        frame = pack_udp_frame("1.1.1.1", 5353, b"payload")
        r = FrameReader()
        for byte in frame:
            r.feed(bytes([byte]))
            mid = list(r.frames())
            if mid:   # 一定是最后一字节进来时才能解出
                self.assertEqual(mid, [("1.1.1.1", 5353, b"payload")])
                break
        else:
            self.fail("frame 未在喂完后被解出")

    def test_partial_buffer_held_for_more_data(self):
        # 喂半帧 → 0 输出；补齐后才出
        frame = pack_udp_frame("1.1.1.1", 53, b"X" * 100)
        r = FrameReader()
        half = len(frame) // 2
        r.feed(frame[:half])
        self.assertEqual(list(r.frames()), [])
        r.feed(frame[half:])
        self.assertEqual(list(r.frames()), [("1.1.1.1", 53, b"X" * 100)])

    def test_empty_payload(self):
        frame = pack_udp_frame("1.1.1.1", 53, b"")
        r = FrameReader()
        r.feed(frame)
        out = list(r.frames())
        self.assertEqual(out, [("1.1.1.1", 53, b"")])


class TestSocks5UdpHeader(unittest.TestCase):
    """build_socks5_udp_header ↔ parse_socks5_udp_header 对称。"""

    def test_ipv4_roundtrip(self):
        hdr = build_socks5_udp_header("203.0.113.7", 53)
        out = parse_socks5_udp_header(hdr + b"PAYLOAD")
        self.assertEqual(out, ("203.0.113.7", 53, b"PAYLOAD"))

    def test_ipv6_roundtrip(self):
        hdr = build_socks5_udp_header("2001:db8::1", 443)
        out = parse_socks5_udp_header(hdr + b"DATA")
        self.assertEqual(out, ("2001:db8::1", 443, b"DATA"))

    def test_domain_roundtrip(self):
        hdr = build_socks5_udp_header("example.com", 853)
        out = parse_socks5_udp_header(hdr + b"Q")
        self.assertEqual(out, ("example.com", 853, b"Q"))

    def test_empty_payload(self):
        hdr = build_socks5_udp_header("1.1.1.1", 53)
        out = parse_socks5_udp_header(hdr)
        self.assertEqual(out, ("1.1.1.1", 53, b""))

    def test_reject_fragmented(self):
        # FRAG != 0：分片包不支持，必须返 None
        hdr = bytearray(build_socks5_udp_header("1.1.1.1", 53))
        hdr[2] = 1   # FRAG = 1
        self.assertIsNone(parse_socks5_udp_header(bytes(hdr) + b"X"))

    def test_reject_too_short(self):
        # 各种短数据都应安全返 None 而非 crash
        for data in (b"", b"\x00", b"\x00\x00", b"\x00\x00\x00",
                     b"\x00\x00\x00\x01", b"\x00\x00\x00\x01\x01\x02"):
            self.assertIsNone(parse_socks5_udp_header(data),
                              f"应拒短数据 {data!r}")

    def test_reject_unknown_atyp(self):
        # ATYP=0x02 / 0xff 都是非法
        for atyp in (0x00, 0x02, 0x05, 0xff):
            data = bytes([0, 0, 0, atyp]) + b"X" * 10
            self.assertIsNone(parse_socks5_udp_header(data))

    def test_reject_nonzero_rsv(self):
        # RSV 必须 0x0000
        data = bytes([0, 1, 0, 0x01]) + b"\x01\x02\x03\x04\x00\x35"
        self.assertIsNone(parse_socks5_udp_header(data))


class TestIpHelpers(unittest.TestCase):
    def test_is_ip_literal_v4(self):
        self.assertTrue(_is_ip_literal("1.2.3.4"))
        self.assertTrue(_is_ip_literal("127.0.0.1"))

    def test_is_ip_literal_v6(self):
        self.assertTrue(_is_ip_literal("::1"))
        self.assertTrue(_is_ip_literal("2001:db8::1"))

    def test_is_ip_literal_domain(self):
        self.assertFalse(_is_ip_literal("example.com"))
        self.assertFalse(_is_ip_literal("not-an-ip"))

    def test_unmap_v4(self):
        self.assertEqual(_unmap_v4("::ffff:1.2.3.4"), "1.2.3.4")
        # 纯 v6 / v4 / 域名都原样返回
        self.assertEqual(_unmap_v4("2001:db8::1"), "2001:db8::1")
        self.assertEqual(_unmap_v4("1.2.3.4"), "1.2.3.4")
        self.assertEqual(_unmap_v4("example.com"), "example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
