"""
TLS ClientHello 构造器

每次连接从三种浏览器指纹档案中随机选择一种，
避免固定 JA3 指纹成为可统计识别的流量标识：

  Chrome 120 — 密码套件、扩展、groups、key_share 均含 GREASE（RFC 8701）
  Firefox 121 — 无 GREASE；ChaCha20 + CBC 套件；含 compress_certificate / record_size_limit
  Safari 17   — 无 GREASE；扩展以 renegotiation_info 开头；更多 ECDSA 套件

三档案主要差异（决定 JA3 哈希值）：
  - 密码套件列表及顺序
  - 扩展类型集合及排列顺序
  - supported_groups 成员列表
  - GREASE 使用与否
"""

from __future__ import annotations

import os
import random
import struct

_GREASE_VALUES = [
    0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
    0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
]


def _grease() -> int:
    return random.choice(_GREASE_VALUES)


def _ext(ext_type: int, data: bytes) -> bytes:
    return struct.pack("!HH", ext_type, len(data)) + data


def _sni_ext(sni_bytes: bytes) -> bytes:
    entry = struct.pack("!BH", 0, len(sni_bytes)) + sni_bytes
    return _ext(0x0000, struct.pack("!H", len(entry)) + entry)


def _alpn_ext() -> bytes:
    # h2 + http/1.1；RFC 7301: 1-byte per-protocol length prefix
    protos = b"\x02h2\x08http/1.1"
    return _ext(0x0010, struct.pack("!H", len(protos)) + protos)


def _key_share_ext(grease_val: int | None) -> bytes:
    eph_pub = os.urandom(32)
    x25519_ks = struct.pack("!HH", 0x001D, 32) + eph_pub
    if grease_val is not None:
        grease_ks = struct.pack("!HH", grease_val, 1) + b"\x00"
        ks_list = grease_ks + x25519_ks
    else:
        ks_list = x25519_ks
    return _ext(0x0033, struct.pack("!H", len(ks_list)) + ks_list)


def _assemble(session_id: bytes, client_random: bytes,
              cipher_suites: bytes, extensions: bytes) -> bytes:
    hello_body = (
        b"\x03\x03"
        + client_random
        + bytes([len(session_id)]) + session_id
        + struct.pack("!H", len(cipher_suites)) + cipher_suites
        + b"\x01\x00"
        + struct.pack("!H", len(extensions)) + extensions
    )
    hs = b"\x01" + struct.pack("!I", len(hello_body))[1:] + hello_body
    return b"\x16\x03\x01" + struct.pack("!H", len(hs)) + hs


# ── 指纹档案 ───────────────────────────────────────────────────────────────────

def _chrome(sni_bytes: bytes, session_id: bytes, client_random: bytes) -> bytes:
    """Chrome 120 — GREASE 遍布密码套件、扩展列表、groups、key_share"""
    gv = _grease()

    ciphers = struct.pack("!10H",
        gv,     0x1301, 0x1302, 0x1303,
        0xC02B, 0xC02F, 0xC02C, 0xC030, 0x009C, 0x00FF,
    )
    groups  = struct.pack("!4H", gv, 0x001D, 0x0017, 0x0018)
    sigalgs = struct.pack("!9H",
        0x0403, 0x0804, 0x0401, 0x0503, 0x0805,
        0x0501, 0x0806, 0x0601, 0x0201,
    )

    exts = (
        _ext(gv, b"\x00")                                             # GREASE（首位）
        + _sni_ext(sni_bytes)
        + _ext(0x0017, b"")                                           # extended_master_secret
        + _ext(0xFF01, b"\x00")                                       # renegotiation_info
        + _ext(0x000A, struct.pack("!H", len(groups)) + groups)       # supported_groups
        + _ext(0x000B, b"\x01\x00")                                   # ec_point_formats
        + _ext(0x0023, b"")                                           # session_ticket
        + _ext(0x0005, b"\x01\x00\x00\x00\x00")                      # status_request
        + _alpn_ext()
        + _ext(0x000D, struct.pack("!H", len(sigalgs)) + sigalgs)    # signature_algorithms
        + _key_share_ext(gv)
        + _ext(0x002D, b"\x01\x01")                                   # psk_key_exchange_modes
        + _ext(0x002B, b"\x04\x03\x04\x03\x03")                      # supported_versions: TLS1.3+1.2
        + _ext(gv, b"\x00")                                           # GREASE（末位）
    )
    return _assemble(session_id, client_random, ciphers, exts)


def _firefox(sni_bytes: bytes, session_id: bytes, client_random: bytes) -> bytes:
    """Firefox 121 — 无 GREASE；ChaCha20 + CBC 套件；含 compress_certificate / record_size_limit"""
    ciphers = struct.pack("!12H",
        0x1301, 0x1303, 0x1302,
        0xC02B, 0xC02F, 0xCCA9, 0xCCA8, 0xC02C, 0xC030,
        0xC009, 0xC013, 0x009C,
    )
    # Firefox 包含 ffdhe 组（有限域 DH），Chrome/Safari 不包含
    groups  = struct.pack("!6H", 0x001D, 0x0017, 0x0018, 0x0019, 0x0100, 0x0101)
    sigalgs = struct.pack("!11H",
        0x0403, 0x0503, 0x0603, 0x0804, 0x0805, 0x0806,
        0x0401, 0x0501, 0x0601, 0x0201, 0x0203,
    )

    exts = (
        _sni_ext(sni_bytes)
        + _ext(0x0017, b"")
        + _ext(0xFF01, b"\x00")
        + _ext(0x000A, struct.pack("!H", len(groups)) + groups)
        + _ext(0x000B, b"\x01\x00")
        + _ext(0x0023, b"")
        + _alpn_ext()
        + _ext(0x0005, b"\x01\x00\x00\x00\x00")
        + _ext(0x000D, struct.pack("!H", len(sigalgs)) + sigalgs)
        + _key_share_ext(None)
        + _ext(0x002D, b"\x01\x01")
        + _ext(0x002B, b"\x04\x03\x04\x03\x03")
        + _ext(0x001B, b"\x02\x00\x02")            # compress_certificate: brotli (0x0002)
        + _ext(0x0031, b"")                         # post_handshake_auth
        + _ext(0x001C, b"\x40\x01")                 # record_size_limit: 16385
    )
    return _assemble(session_id, client_random, ciphers, exts)


def _safari(sni_bytes: bytes, session_id: bytes, client_random: bytes) -> bytes:
    """Safari 17 — 无 GREASE；renegotiation_info 位于扩展首位；更多 ECDSA 套件"""
    ciphers = struct.pack("!15H",
        0x1301, 0x1302, 0x1303,
        0xC02C, 0xC02B, 0xC030, 0xC02F, 0xCCA9, 0xCCA8,
        0xC024, 0xC023, 0xC00A, 0xC009, 0x009D, 0x009C,
    )
    groups  = struct.pack("!4H", 0x001D, 0x0017, 0x0018, 0x0019)
    sigalgs = struct.pack("!11H",
        0x0403, 0x0804, 0x0401, 0x0503, 0x0603,
        0x0805, 0x0806, 0x0501, 0x0601, 0x0203, 0x0201,
    )

    exts = (
        _ext(0xFF01, b"\x00")                                         # renegotiation_info（Safari 首位）
        + _sni_ext(sni_bytes)
        + _ext(0x0017, b"")
        + _ext(0x0023, b"")
        + _ext(0x000D, struct.pack("!H", len(sigalgs)) + sigalgs)
        + _ext(0x0005, b"\x01\x00\x00\x00\x00")
        + _alpn_ext()
        + _ext(0x000A, struct.pack("!H", len(groups)) + groups)
        + _ext(0x000B, b"\x01\x00")
        + _ext(0x001B, b"\x04\x00\x01\x00\x02")    # compress_certificate: zlib(0x0001)+brotli(0x0002)
        + _ext(0x0031, b"")
        + _key_share_ext(None)
        + _ext(0x002D, b"\x01\x01")
        + _ext(0x002B, b"\x04\x03\x04\x03\x03")
    )
    return _assemble(session_id, client_random, ciphers, exts)


_BUILDERS = [_chrome, _firefox, _safari]


# ── 公共接口 ───────────────────────────────────────────────────────────────────

def build_client_hello(server_name: str, session_id: bytes) -> tuple[bytes, bytes]:
    """
    构造 TLS ClientHello 记录，每次随机选择 Chrome / Firefox / Safari 指纹档案。

    参数：
      server_name: SNI 域名（即伪装站点，如 "www.apple.com"）
      session_id:  32 字节，嵌入认证 token

    返回：(record_bytes, client_random)
      record_bytes: 完整的 TLS 记录，可直接写入 TCP
      client_random: 32 字节，双方用此派生会话密钥
    """
    assert len(session_id) == 32, "session_id 必须是 32 字节"
    client_random = os.urandom(32)
    sni_bytes = server_name.encode()
    return random.choice(_BUILDERS)(sni_bytes, session_id, client_random), client_random


def build_fake_client_tail() -> bytes:
    """
    构造 TLS 1.3 客户端握手 flight：ChangeCipherSpec（compat record）+ 假 Finished。
    """
    ccs = b"\x14\x03\x03\x00\x01\x01"
    finished_body = os.urandom(52)
    return ccs + b"\x17\x03\x03" + struct.pack("!H", len(finished_body)) + finished_body
