"""
手动构造 TLS 1.3 ClientHello

Python 的 ssl 模块不允许自定义 legacy_session_id，
所以客户端必须手动构造 ClientHello 字节，通过原始 TCP 发送。
服务端会从中读取 session_id 并验证身份。

构造的 ClientHello 包含所有标准扩展，看起来与正常浏览器流量一致：
  - server_name (SNI)
  - supported_versions (TLS 1.3 + 1.2)
  - supported_groups (x25519, secp256r1)
  - key_share (x25519 临时公钥)
  - signature_algorithms
  - extended_master_secret
  - session_ticket (空)
"""

from __future__ import annotations

import os
import struct


def _ext(ext_type: int, data: bytes) -> bytes:
    """构造一个 TLS 扩展：[2字节类型][2字节长度][数据]"""
    return struct.pack("!HH", ext_type, len(data)) + data


def build_client_hello(server_name: str, session_id: bytes) -> bytes:
    """
    构造完整的 TLS ClientHello 记录字节。

    参数：
      server_name: SNI 域名（即伪装站点，如 "www.apple.com"）
      session_id:  32 字节，嵌入认证 token
    """
    assert len(session_id) == 32, "session_id 必须是 32 字节"

    client_random = os.urandom(32)

    # ── 密码套件（TLS 1.3 标准套件）────────────────────────────────────────
    cipher_suites = struct.pack("!8H",
        0x1301,  # TLS_AES_128_GCM_SHA256
        0x1302,  # TLS_AES_256_GCM_SHA384
        0x1303,  # TLS_CHACHA20_POLY1305_SHA256
        0xC02B,  # TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256（TLS 1.2 兼容）
        0xC02F,  # TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        0xC02C,  # TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        0xC030,  # TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        0x00FF,  # TLS_EMPTY_RENEGOTIATION_INFO_SCSV
    )

    # ── 扩展 ─────────────────────────────────────────────────────────────────

    # SNI
    sni_name = server_name.encode()
    sni_data = (struct.pack("!BH", 0, len(sni_name)) + sni_name)
    sni_list = struct.pack("!H", len(sni_data)) + sni_data
    ext_sni = _ext(0x0000, sni_list)

    # extended_master_secret（空扩展，增加真实感）
    ext_ems = _ext(0x0017, b"")

    # session_ticket（空，表示没有复用 ticket）
    ext_ticket = _ext(0x0023, b"")

    # supported_groups
    groups = struct.pack("!3H", 0x001d, 0x0017, 0x0018)  # x25519, secp256r1, secp384r1
    ext_groups = _ext(0x000a, struct.pack("!H", len(groups)) + groups)

    # signature_algorithms
    sig_algs = struct.pack("!8H",
        0x0403,  # ecdsa_secp256r1_sha256
        0x0804,  # rsa_pss_rsae_sha256
        0x0401,  # rsa_pkcs1_sha256
        0x0503,  # ecdsa_secp384r1_sha384
        0x0805,  # rsa_pss_rsae_sha384
        0x0501,  # rsa_pkcs1_sha384
        0x0806,  # rsa_pss_rsae_sha512
        0x0601,  # rsa_pkcs1_sha512
    )
    ext_sigalg = _ext(0x000d, struct.pack("!H", len(sig_algs)) + sig_algs)

    # supported_versions（告知服务端支持 TLS 1.3）
    versions = struct.pack("!BHH", 4, 0x0304, 0x0303)  # length=4, TLS1.3, TLS1.2
    ext_versions = _ext(0x002b, versions)

    # key_share（x25519 临时密钥，随机生成——我们不会真正完成 TLS 握手）
    eph_pub = os.urandom(32)
    ks_entry = struct.pack("!HH", 0x001d, 32) + eph_pub  # group=x25519, len=32
    ks_list = struct.pack("!H", len(ks_entry)) + ks_entry
    ext_keyshare = _ext(0x0033, ks_list)

    all_extensions = (
        ext_sni + ext_ems + ext_ticket +
        ext_groups + ext_sigalg + ext_versions + ext_keyshare
    )

    # ── ClientHello 主体 ────────────────────────────────────────────────────
    hello_body = (
        b"\x03\x03"                               # legacy_version = TLS 1.2
        + client_random                            # 32 字节随机数
        + bytes([len(session_id)]) + session_id   # session_id（嵌入认证 token）
        + struct.pack("!H", len(cipher_suites)) + cipher_suites
        + b"\x01\x00"                             # compression_methods: [null]
        + struct.pack("!H", len(all_extensions)) + all_extensions
    )

    # ── Handshake 消息包装 ──────────────────────────────────────────────────
    hs_len = len(hello_body)
    handshake = (
        b"\x01"                                   # HandshakeType = ClientHello
        + struct.pack("!I", hs_len)[1:]           # 3 字节长度
        + hello_body
    )

    # ── TLS 记录层包装 ──────────────────────────────────────────────────────
    record = (
        b"\x16\x03\x01"                           # content_type=Handshake, legacy_version=TLS1.0
        + struct.pack("!H", len(handshake))
        + handshake
    )

    return record
