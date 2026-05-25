"""
流量嗅探：从 TCP 载荷提取域名

支持：
  TLS 1.x ClientHello → SNI 扩展（type 0x0000）中的 server_name
  HTTP/1.x 请求       → Host: 请求头

用于 TProxy 模式：透明代理只能从 sockname 拿到目标 IP，通过 sniff 提取域名后
可以使用 GEOSITE / DOMAIN-SUFFIX 等规则进行精确分流，而不必退化为仅 IP 规则。
"""

from __future__ import annotations

import asyncio

_SNIFF_BYTES   = 1024  # 覆盖绝大多数 TLS ClientHello（SNI 通常在前 512 字节内）
_SNIFF_TIMEOUT = 2.0   # 等待初始数据的超时（秒），避免慢客户端阻塞接受循环


# ── TLS SNI ────────────────────────────────────────────────────────────────────

def sniff_tls_sni(data: bytes) -> str | None:
    """从 TLS ClientHello 记录中提取 SNI server_name（明文，TLS 1.3 同样适用）"""
    try:
        if len(data) < 5 or data[0] != 0x16:         # 不是 Handshake 记录
            return None
        hs = data[5:]
        if not hs or hs[0] != 0x01:                   # 不是 ClientHello
            return None

        hs_len = int.from_bytes(hs[1:4], "big")
        hello  = hs[4: 4 + hs_len]

        # version(2) + random(32) = 34 字节固定头
        offset = 34
        if offset >= len(hello):
            return None

        # session_id（长度可变）
        sid_len = hello[offset]
        offset += 1 + sid_len

        # cipher_suites（长度可变）
        if offset + 2 > len(hello):
            return None
        cs_len = int.from_bytes(hello[offset: offset + 2], "big")
        offset += 2 + cs_len

        # compression_methods（长度可变）
        if offset + 1 > len(hello):
            return None
        cm_len = hello[offset]
        offset += 1 + cm_len

        # extensions 总长度
        if offset + 2 > len(hello):
            return None
        ext_end = offset + 2 + int.from_bytes(hello[offset: offset + 2], "big")
        offset += 2

        # 遍历扩展，寻找 SNI（type 0x0000）
        while offset + 4 <= ext_end:
            ext_type = int.from_bytes(hello[offset:     offset + 2], "big")
            ext_len  = int.from_bytes(hello[offset + 2: offset + 4], "big")
            ext_data = hello[offset + 4: offset + 4 + ext_len]
            offset  += 4 + ext_len

            if ext_type == 0x0000 and len(ext_data) >= 5:
                # server_name_list_len(2) + name_type(1) + name_len(2) + name
                name_len = int.from_bytes(ext_data[3:5], "big")
                if len(ext_data) >= 5 + name_len:
                    return ext_data[5: 5 + name_len].decode("ascii", errors="replace").lower()

        return None
    except Exception:
        return None


# ── HTTP Host ──────────────────────────────────────────────────────────────────

def sniff_http_host(data: bytes) -> str | None:
    """从 HTTP/1.x 请求头中提取 Host 字段值（不含端口号）"""
    try:
        # 只看头部，不需要等到 body
        header_part = data.split(b"\r\n\r\n", 1)[0]
        text = header_part.decode("ascii", errors="replace")
        for line in text.split("\r\n")[1:]:    # 跳过请求行
            if line.lower().startswith("host:"):
                host = line[5:].strip()
                return host.rsplit(":", 1)[0].lower()   # 去掉端口
        return None
    except Exception:
        return None


# ── 统一嗅探接口 ───────────────────────────────────────────────────────────────

async def sniff_domain(
    reader: asyncio.StreamReader,
    timeout: float = _SNIFF_TIMEOUT,
) -> tuple[str | None, bytes]:
    """
    从 StreamReader 读取初始字节并提取域名。

    返回 (domain, buffered)：
      domain    提取到的域名（TLS SNI 或 HTTP Host），失败则 None
      buffered  已读出的字节，必须通过 PrefixedReader 还给后续逻辑

    不会抛出异常：任何错误都退化为 (None, b"")。
    """
    try:
        data = await asyncio.wait_for(reader.read(_SNIFF_BYTES), timeout=timeout)
    except Exception:
        return None, b""

    domain = sniff_tls_sni(data) or sniff_http_host(data)
    return domain, data


# ── PrefixedReader ─────────────────────────────────────────────────────────────

class PrefixedReader:
    """
    将已读出的字节"放回"到 reader 前面，对上层调用方完全透明。

    实现了 _dispatch / relay 实际使用的接口：read()。
    前缀字节耗尽后自动委托给原始 StreamReader。
    """

    __slots__ = ("_reader", "_prefix", "_pos")

    def __init__(self, reader: asyncio.StreamReader, prefix: bytes) -> None:
        self._reader = reader
        self._prefix = prefix
        self._pos    = 0

    async def read(self, n: int = -1) -> bytes:
        remaining = self._prefix[self._pos:]
        if remaining:
            chunk = remaining if (n < 0 or n >= len(remaining)) else remaining[:n]
            self._pos += len(chunk)
            return chunk
        return await self._reader.read(n)
