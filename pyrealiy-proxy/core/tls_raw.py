"""
手动构造 TLS 1.2 ClientHello

Python 的 ssl 模块不允许自定义 legacy_session_id，
所以客户端必须手动构造 ClientHello 字节，通过原始 TCP 发送。
服务端从中读取 session_id 并验证身份，同时提取 client_random 用于派生会话密钥。

构造的 ClientHello 模仿 Chrome 浏览器指纹：
  - 密码套件列表以 GREASE 开头（RFC 8701）
  - 扩展顺序与 Chrome 一致
  - 包含 GREASE 扩展（首尾各一个）
  - 包含 ALPN、OCSP stapling、renegotiation_info、ec_point_formats 等标准扩展
"""

from __future__ import annotations

import os
import random
import struct

# GREASE 保留值（RFC 8701 §3.1）
_GREASE_VALUES = [
    0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
    0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
]


def _grease() -> int:
    return random.choice(_GREASE_VALUES)


def _ext(ext_type: int, data: bytes) -> bytes:
    return struct.pack("!HH", ext_type, len(data)) + data


def build_client_hello(server_name: str, session_id: bytes) -> tuple[bytes, bytes]:
    """
    构造完整的 TLS ClientHello 记录字节，模仿 Chrome 浏览器指纹。

    参数：
      server_name: SNI 域名（即伪装站点，如 "www.apple.com"）
      session_id:  32 字节，嵌入认证 token

    返回：(record_bytes, client_random)
      record_bytes: 完整的 TLS 记录，可直接写入 TCP
      client_random: 32 字节，双方用此派生会话密钥
    """
    assert len(session_id) == 32, "session_id 必须是 32 字节"

    client_random = os.urandom(32)
    grease_val = _grease()

    # ── 密码套件（Chrome-like，GREASE 开头）─────────────────────────────────
    cipher_suites = struct.pack("!10H",
        grease_val,  # GREASE
        0x1301,      # TLS_AES_128_GCM_SHA256
        0x1302,      # TLS_AES_256_GCM_SHA384
        0x1303,      # TLS_CHACHA20_POLY1305_SHA256
        0xC02B,      # TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        0xC02F,      # TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        0xC02C,      # TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        0xC030,      # TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        0x009C,      # TLS_RSA_WITH_AES_128_GCM_SHA256
        0x00FF,      # TLS_EMPTY_RENEGOTIATION_INFO_SCSV
    )

    # ── 扩展（Chrome 扩展顺序）──────────────────────────────────────────────

    # GREASE（首位）
    ext_grease_first = _ext(grease_val, b"\x00")

    # SNI
    sni = server_name.encode()
    sni_entry = struct.pack("!BH", 0, len(sni)) + sni
    ext_sni = _ext(0x0000, struct.pack("!H", len(sni_entry)) + sni_entry)

    # extended_master_secret
    ext_ems = _ext(0x0017, b"")

    # renegotiation_info（新连接，info 为空）
    ext_reneg = _ext(0xFF01, b"\x00")

    # supported_groups（GREASE + x25519 + secp256r1 + secp384r1）
    groups = struct.pack("!4H", grease_val, 0x001D, 0x0017, 0x0018)
    ext_groups = _ext(0x000A, struct.pack("!H", len(groups)) + groups)

    # ec_point_formats（仅 uncompressed）
    ext_ecpf = _ext(0x000B, b"\x01\x00")

    # session_ticket（空，无复用）
    ext_ticket = _ext(0x0023, b"")

    # status_request（OCSP stapling）
    ext_status = _ext(0x0005, b"\x01\x00\x00\x00\x00")

    # ALPN（h2 + http/1.1，与 Chrome 访问 HTTPS 一致）
    alpn_protos = b"\x00\x02h2\x00\x08http/1.1"
    ext_alpn = _ext(0x0010, struct.pack("!H", len(alpn_protos)) + alpn_protos)

    # signature_algorithms
    sig_algs = struct.pack("!9H",
        0x0403,  # ecdsa_secp256r1_sha256
        0x0804,  # rsa_pss_rsae_sha256
        0x0401,  # rsa_pkcs1_sha256
        0x0503,  # ecdsa_secp384r1_sha384
        0x0805,  # rsa_pss_rsae_sha384
        0x0501,  # rsa_pkcs1_sha384
        0x0806,  # rsa_pss_rsae_sha512
        0x0601,  # rsa_pkcs1_sha512
        0x0201,  # rsa_pkcs1_sha1
    )
    ext_sigalg = _ext(0x000D, struct.pack("!H", len(sig_algs)) + sig_algs)

    # key_share（GREASE entry + x25519，与 Chrome 一致）
    eph_pub        = os.urandom(32)
    grease_ks      = struct.pack("!HH", grease_val, 1) + b"\x00"   # GREASE group, 1-byte key
    x25519_ks      = struct.pack("!HH", 0x001D, 32) + eph_pub
    ks_list        = grease_ks + x25519_ks
    ext_keyshare   = _ext(0x0033, struct.pack("!H", len(ks_list)) + ks_list)

    # psk_key_exchange_modes（psk_dhe_ke）
    ext_psk_modes = _ext(0x002D, b"\x01\x01")

    # supported_versions（TLS 1.3 + 1.2）
    ext_versions = _ext(0x002B, b"\x04\x03\x04\x03\x03")

    # GREASE（末位）
    ext_grease_last = _ext(grease_val, b"\x00")

    all_extensions = (
        ext_grease_first + ext_sni + ext_ems + ext_reneg +
        ext_groups + ext_ecpf + ext_ticket + ext_status + ext_alpn +
        ext_sigalg + ext_keyshare + ext_psk_modes + ext_versions +
        ext_grease_last
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

    hs_len = len(hello_body)
    handshake = (
        b"\x01"                        # HandshakeType = ClientHello
        + struct.pack("!I", hs_len)[1:]  # 3 字节长度
        + hello_body
    )

    record = (
        b"\x16\x03\x01"               # content_type=Handshake, legacy_version=TLS1.0
        + struct.pack("!H", len(handshake))
        + handshake
    )

    return record, client_random


def build_fake_client_tail() -> bytes:
    """
    构造 TLS 1.3 客户端在收到服务端 flight 后应发送的假记录：
      ChangeCipherSpec（compat middlebox record）+ Finished（Encrypted Application Data）

    TLS 1.3 无 ClientKeyExchange，密钥协商通过 ClientHello 中的 key_share 完成。
    服务端读取这两条记录后丢弃，双方直接进入加密应用数据模式。
    """
    # ChangeCipherSpec（TLS 1.3 compat record，Chrome 发送）
    ccs = b"\x14\x03\x03\x00\x01\x01"

    # Finished：TLS 1.3 中为 type=0x17（Encrypted Application Data）
    # 大小约 52 字节（4字节 HMAC header + 32字节 verify_data + 16字节 AEAD tag）
    finished_body = os.urandom(52)
    finished = b"\x17\x03\x03" + struct.pack("!H", len(finished_body)) + finished_body

    return ccs + finished
