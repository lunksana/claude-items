"""
DNS 转发器单元测试：报文解析 + 地址解析（处理不可信网络输入，最易藏 bug）。

覆盖：
  - _extract_domain：从 DNS 查询提 QNAME（正常 / 畸形 / 空 / 截断）
  - _question_end：question 段定位（多 question / 截断）
  - _nxdomain：QR=1 RCODE=3，保留 question，清 answer 计数
  - _parse_udp_addr：cn_dns host:port / 裸 v6 / [v6]:port / scheme 剥离

真 socket 查询（_udp_query / 隧道）留给集成，不在这里。
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dns_forwarder import (  # noqa: E402
    _extract_domain, _question_end, _nxdomain, _parse_udp_addr,
)


def _build_query(domain: str, qtype: int = 1, tx_id: int = 0x1234,
                 qdcount: int = 1) -> bytes:
    """构造一个最小 DNS 查询报文。"""
    header = struct.pack("!HHHHHH", tx_id, 0x0100, qdcount, 0, 0, 0)
    q = b""
    for label in domain.split("."):
        q += bytes([len(label)]) + label.encode("ascii")
    q += b"\x00"                          # QNAME 结束
    q += struct.pack("!HH", qtype, 1)     # QTYPE + QCLASS=IN
    return header + q


class TestExtractDomain(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(_extract_domain(_build_query("example.com")), "example.com")

    def test_subdomain(self):
        self.assertEqual(_extract_domain(_build_query("a.b.c.example.com")),
                         "a.b.c.example.com")

    def test_lowercased(self):
        self.assertEqual(_extract_domain(_build_query("ExAmPle.COM")), "example.com")

    def test_root_query_returns_none(self):
        # QNAME = "."（仅一个 0 字节）→ 无 label
        header = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        pkt = header + b"\x00" + struct.pack("!HH", 1, 1)
        self.assertIsNone(_extract_domain(pkt))

    def test_truncated_no_crash(self):
        full = _build_query("example.com")
        for cut in range(13, len(full)):
            # 截断不应抛异常（返回部分或 None）
            _extract_domain(full[:cut])

    def test_garbage_no_crash(self):
        for data in (b"", b"\x00" * 5, b"\xff" * 20, bytes(range(40))):
            _extract_domain(data)   # 不抛即可

    def test_compression_pointer_in_question_stops(self):
        # question 段不应有指针压缩；遇 0xC0 应停（不无限循环 / 不崩）
        header = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        pkt = header + b"\x03abc\xc0\x05" + struct.pack("!HH", 1, 1)
        _extract_domain(pkt)   # 不抛即可


class TestQuestionEnd(unittest.TestCase):
    def test_single_question(self):
        pkt = _build_query("example.com")
        # question 段后应正好是报文末尾（无 answer）
        self.assertEqual(_question_end(pkt), len(pkt))

    def test_truncated_returns_len(self):
        pkt = _build_query("example.com")[:20]
        self.assertEqual(_question_end(pkt), len(pkt))

    def test_question_end_before_trailing_data(self):
        # 在 question 后加 EDNS0-ish 尾巴；_question_end 应停在 question 末尾
        pkt = _build_query("x.com")
        end = _question_end(pkt)
        padded = pkt + b"\x00\x00\x29\x10\x00"   # 假 OPT 记录尾巴
        self.assertEqual(_question_end(padded), end)


class TestNxdomain(unittest.TestCase):
    def test_qr_and_rcode_set(self):
        q = _build_query("example.com")
        resp = _nxdomain(q)
        flags = struct.unpack("!H", resp[2:4])[0]
        self.assertTrue(flags & 0x8000, "QR 位应置 1")
        self.assertEqual(flags & 0x000F, 3, "RCODE 应为 3 (NXDOMAIN)")

    def test_preserves_tx_id_and_question(self):
        q = _build_query("example.com", tx_id=0xABCD)
        resp = _nxdomain(q)
        self.assertEqual(resp[:2], q[:2])                  # tx_id 保留
        self.assertEqual(_extract_domain(resp), "example.com")  # question 保留

    def test_answer_counts_zeroed(self):
        q = _build_query("example.com")
        resp = _nxdomain(q)
        qd, an, ns, ar = struct.unpack("!HHHH", resp[4:12])
        self.assertEqual(qd, 1)   # 保留 1 question
        self.assertEqual((an, ns, ar), (0, 0, 0))

    def test_strips_trailing_additional(self):
        # 带 OPT 尾巴的查询 → NXDOMAIN 应只保留 question（ARCOUNT=0 时不能带尾巴）
        q = _build_query("example.com") + b"\x00\x00\x29\x10\x00\x00\x00\x00\x00"
        resp = _nxdomain(q)
        _, _, _, ar = struct.unpack("!HHHH", resp[4:12])
        self.assertEqual(ar, 0)
        # 响应长度 = 头(12) + question，不含尾巴
        self.assertEqual(len(resp), _question_end(q))

    def test_short_input_returned_asis(self):
        self.assertEqual(_nxdomain(b"\x01\x02"), b"\x01\x02")


class TestParseUdpAddr(unittest.TestCase):
    def test_bare_v4(self):
        self.assertEqual(_parse_udp_addr("223.5.5.5"), ("223.5.5.5", 53))

    def test_v4_with_port(self):
        self.assertEqual(_parse_udp_addr("223.5.5.5:5353"), ("223.5.5.5", 5353))

    def test_bare_v6(self):
        self.assertEqual(_parse_udp_addr("2400:3200::1"), ("2400:3200::1", 53))

    def test_v6_bracketed_with_port(self):
        self.assertEqual(_parse_udp_addr("[2400:3200::1]:5353"),
                         ("2400:3200::1", 5353))

    def test_v6_bracketed_no_port(self):
        self.assertEqual(_parse_udp_addr("[2400:3200::1]"), ("2400:3200::1", 53))

    def test_strips_tls_scheme(self):
        # full_proxy preset 把 cn 设成 remote（可能带 scheme）→ cn 路径只 UDP，
        # 必须剥 scheme，否则把整串当主机名解析失败
        self.assertEqual(_parse_udp_addr("tls://1.1.1.1:853"), ("1.1.1.1", 853))

    def test_strips_https_scheme_and_path(self):
        self.assertEqual(_parse_udp_addr("https://1.1.1.1/dns-query"),
                         ("1.1.1.1", 53))

    def test_custom_default_port(self):
        self.assertEqual(_parse_udp_addr("8.8.8.8", default_port=5300),
                         ("8.8.8.8", 5300))


if __name__ == "__main__":
    unittest.main(verbosity=2)
