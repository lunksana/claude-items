"""
TLS ClientHello 字节构造（伪装指纹）测试。

这是 wire 契约里最容易出字节级偏差的一块：任何长度域 off-by-one 都会让真实
TLS 解析器（GFW 探测器 / 我们自己的服务端 camouflage）判 malformed，伪装直接
破功。本测试内置一个**严格的 ClientHello 解析器**逐字段校验所有长度域，并与
hello_auth 的提取器做跨模块 round-trip（服务端正是用那套提取 token / random）。

Rust 完全体重写 tls_raw 时，这套测试就是字节级行为基准。
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tls_raw  # noqa: E402
from core.tls_raw import build_client_hello, build_fake_client_tail  # noqa: E402
from core.hello_auth import extract_session_id, extract_client_random  # noqa: E402


def _parse_client_hello(record: bytes) -> dict:
    """
    严格解析 TLS ClientHello 记录，校验每个长度域自洽，返回拆出的字段。
    任何长度不匹配 / 越界都 raise AssertionError（= 字节级 bug）。
    """
    # ── 外层 TLS record ──
    assert record[0] == 0x16, f"record type 应为 Handshake(0x16)，得 {record[0]:#x}"
    assert record[1:3] == b"\x03\x01", "legacy_record_version 应为 0x0301"
    rec_len = struct.unpack("!H", record[3:5])[0]
    assert rec_len == len(record) - 5, f"record 长度域 {rec_len} != 实际 {len(record)-5}"

    # ── handshake message ──
    hs = record[5:]
    assert hs[0] == 0x01, "handshake type 应为 ClientHello(0x01)"
    hs_len = int.from_bytes(hs[1:4], "big")
    assert hs_len == len(hs) - 4, f"handshake 长度域 {hs_len} != 实际 {len(hs)-4}"

    body = hs[4:]
    off = 0
    assert body[off:off + 2] == b"\x03\x03", "legacy_version 应为 0x0303"
    off += 2

    client_random = body[off:off + 32]
    assert len(client_random) == 32
    off += 32

    sid_len = body[off]; off += 1
    session_id = body[off:off + sid_len]
    assert len(session_id) == sid_len, "session_id 截断"
    off += sid_len

    cs_len = struct.unpack("!H", body[off:off + 2])[0]; off += 2
    assert cs_len % 2 == 0, "cipher_suites 长度应为偶数（每套件 2 字节）"
    cipher_suites = body[off:off + cs_len]
    assert len(cipher_suites) == cs_len, "cipher_suites 截断"
    off += cs_len

    comp_len = body[off]; off += 1
    assert comp_len == 1, "compression_methods 长度应为 1"
    assert body[off] == 0x00, "compression 应为 null(0x00)"
    off += comp_len

    ext_total = struct.unpack("!H", body[off:off + 2])[0]; off += 2
    ext_block = body[off:off + ext_total]
    assert len(ext_block) == ext_total, f"extensions 长度域 {ext_total} != 实际 {len(ext_block)}"
    off += ext_total
    assert off == len(body), f"ClientHello 末尾有 {len(body)-off} 字节多余数据"

    # ── 逐扩展走 TLV，校验每个长度域自洽，直到正好走完 ──
    exts = []
    eo = 0
    while eo < len(ext_block):
        assert eo + 4 <= len(ext_block), "扩展头被截断"
        etype = struct.unpack("!H", ext_block[eo:eo + 2])[0]
        elen = struct.unpack("!H", ext_block[eo + 2:eo + 4])[0]
        eo += 4
        assert eo + elen <= len(ext_block), f"扩展 {etype:#x} 长度 {elen} 越界"
        exts.append((etype, ext_block[eo:eo + elen]))
        eo += elen
    assert eo == len(ext_block), "扩展块未被正好走完（某扩展长度域错）"

    return {
        "client_random": client_random,
        "session_id": session_id,
        "cipher_suites": cipher_suites,
        "ext_types": [t for t, _ in exts],
        "exts": dict(exts),
    }


_TOKEN = os.urandom(32)


class TestEachFingerprintWellFormed(unittest.TestCase):
    """三档案逐一：完整结构 + 所有长度域自洽。"""

    def _check(self, builder):
        cr = os.urandom(32)
        record = builder(b"www.apple.com", _TOKEN, cr)
        parsed = _parse_client_hello(record)   # 内部 assert 校验所有长度
        self.assertEqual(parsed["session_id"], _TOKEN)
        self.assertEqual(parsed["client_random"], cr)
        return parsed

    def test_chrome(self):
        p = self._check(tls_raw._chrome)
        self.assertIn(0x0000, p["ext_types"])   # SNI
        self.assertIn(0x0033, p["ext_types"])   # key_share

    def test_firefox(self):
        p = self._check(tls_raw._firefox)
        self.assertIn(0x001C, p["ext_types"])   # record_size_limit（Firefox 特征）

    def test_safari(self):
        p = self._check(tls_raw._safari)
        # Safari 扩展首位是 renegotiation_info(0xFF01)
        self.assertEqual(p["ext_types"][0], 0xFF01)


class TestSniEmbedded(unittest.TestCase):
    def test_sni_contains_server_name(self):
        for builder in tls_raw._BUILDERS:
            record = builder(b"www.example.com", _TOKEN, os.urandom(32))
            parsed = _parse_client_hello(record)
            sni_ext = parsed["exts"][0x0000]
            self.assertIn(b"www.example.com", sni_ext,
                          f"{builder.__name__} 的 SNI 未含 server_name")

    def test_alpn_present(self):
        for builder in tls_raw._BUILDERS:
            parsed = _parse_client_hello(builder(b"a.com", _TOKEN, os.urandom(32)))
            self.assertIn(b"h2", parsed["exts"][0x0010])
            self.assertIn(b"http/1.1", parsed["exts"][0x0010])


class TestCrossModuleExtraction(unittest.TestCase):
    """build_client_hello 产出必须能被服务端用的 hello_auth 提取器解析（核心契约）。"""

    def test_roundtrip_via_public_api(self):
        for _ in range(30):   # public API 随机选档案，多跑覆盖三档
            record, cr = build_client_hello("www.apple.com", _TOKEN)
            self.assertEqual(extract_session_id(record), _TOKEN)
            self.assertEqual(extract_client_random(record), cr)

    def test_each_builder_extractable(self):
        for builder in tls_raw._BUILDERS:
            cr = os.urandom(32)
            record = builder(b"www.apple.com", _TOKEN, cr)
            self.assertEqual(extract_session_id(record), _TOKEN, builder.__name__)
            self.assertEqual(extract_client_random(record), cr, builder.__name__)


class TestPublicApi(unittest.TestCase):
    def test_returns_record_and_random(self):
        record, cr = build_client_hello("x.com", _TOKEN)
        self.assertEqual(len(cr), 32)
        self.assertEqual(record[0], 0x16)

    def test_rejects_bad_session_id_len(self):
        with self.assertRaises(AssertionError):
            build_client_hello("x.com", b"\x00" * 31)   # 必须 32 字节

    def test_random_differs_each_call(self):
        _, cr1 = build_client_hello("x.com", _TOKEN)
        _, cr2 = build_client_hello("x.com", _TOKEN)
        self.assertNotEqual(cr1, cr2)


class TestFakeClientTail(unittest.TestCase):
    def test_structure(self):
        tail = build_fake_client_tail()
        # CCS record: 0x14 0x03 0x03 0x00 0x01 0x01
        self.assertEqual(tail[:6], b"\x14\x03\x03\x00\x01\x01")
        # 紧跟一条 application_data record (0x17 0x03 0x03 [len] [body])
        self.assertEqual(tail[6:9], b"\x17\x03\x03")
        body_len = struct.unpack("!H", tail[9:11])[0]
        self.assertEqual(body_len, len(tail) - 11, "Finished record 长度域不符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
