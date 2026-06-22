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

    循环读取直到提取成功 / 达到 _SNIFF_BYTES / 超时 / EOF。
    单次 read() 在 TLS ClientHello 跨 TCP 段分片时会返回不完整数据，
    导致 SNI 提取失败而退化到 IP 路由，损失精度。
    """
    buffered = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while len(buffered) < _SNIFF_BYTES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(
                reader.read(_SNIFF_BYTES - len(buffered)),
                timeout=remaining,
            )
            if not chunk:
                break
            buffered.extend(chunk)
            raw = bytes(buffered)
            domain = sniff_tls_sni(raw) or sniff_http_host(raw)
            if domain:
                return domain, raw
    except Exception:
        pass
    return None, bytes(buffered)


# ── PrefixedReader ─────────────────────────────────────────────────────────────

class PrefixedReader:
    """
    将已读出的字节"放回"到 reader 前面，对上层调用方完全透明。

    实现 `read` / `readexactly` / `readuntil` 三个 StreamReader 风格接口，
    覆盖 SOCKS5 解析（readexactly）、HTTP 请求解析（readuntil）、
    bidi 中继（read）三类调用方式。

    前缀字节耗尽后自动委托给原始 StreamReader。
    """

    __slots__ = ("_reader", "_prefix", "_pos")

    def __init__(self, reader: asyncio.StreamReader, prefix: bytes) -> None:
        self._reader = reader
        self._prefix = bytes(prefix)
        self._pos    = 0

    async def read(self, n: int = -1) -> bytes:
        remaining = self._prefix[self._pos:]
        if remaining:
            chunk = remaining if (n < 0 or n >= len(remaining)) else remaining[:n]
            self._pos += len(chunk)
            return chunk
        return await self._reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        """先吃前缀剩余，再 fall through 到 reader.readexactly。"""
        remaining = self._prefix[self._pos:]
        if len(remaining) >= n:
            self._pos += n
            return bytes(remaining[:n])
        self._pos = len(self._prefix)
        rest = await self._reader.readexactly(n - len(remaining))
        return bytes(remaining) + rest

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """
        关键边界：separator 可能跨"前缀-reader"分界。
        正确做法：找前缀末尾与 separator 头部的最长重合，再向 reader 读够字节
        填满判断窗口。
        """
        remaining = self._prefix[self._pos:]
        # 快路径：separator 完全在前缀里
        idx = remaining.find(separator)
        if idx >= 0:
            end = idx + len(separator)
            self._pos += end
            return bytes(remaining[:end])

        # 排空前缀
        self._pos = len(self._prefix)
        buf = bytearray(remaining)

        # 处理跨界：前缀末尾 K 字节是 separator 的前 K 字节
        sep_len = len(separator)
        if buf and sep_len > 1:
            max_overlap = min(len(buf), sep_len - 1)
            overlap = 0
            for k in range(max_overlap, 0, -1):
                if bytes(buf[-k:]) == separator[:k]:
                    overlap = k
                    break
            if overlap:
                # 再读 sep_len - overlap 字节看是否补齐 separator
                need = sep_len - overlap
                extra = await self._reader.readexactly(need)
                buf.extend(extra)
                idx = buf.find(separator)
                if idx >= 0:
                    end = idx + sep_len
                    if end < len(buf):
                        # 多读的字节回退成新前缀
                        self._prefix = bytes(buf[end:])
                        self._pos = 0
                    return bytes(buf[:end])
                # 这段窗口里没 separator，下面继续从 reader 找

        # 主体：从 reader 找 separator
        remaining_from_reader = await self._reader.readuntil(separator)
        return bytes(buf) + remaining_from_reader

    def at_eof(self) -> bool:
        if self._pos < len(self._prefix):
            return False
        return self._reader.at_eof()
