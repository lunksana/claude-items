"""
DNS 转发器

本地监听 UDP 53/5353，按 router 决定的 outbound tag 分流：
  block leaf    → 返回 NXDOMAIN
  direct leaf   → UDP 直查国内 DNS（如 223.5.5.5）—— UDP 比 TCP 显著省一次握手
  pyrealiy leaf → 经该 outbound 的隧道做 DNS-over-TCP（pipeline，一个 DoT 隧道服多查）

组节点（urltest/fallback）按 resolve_leaf() 选定的叶子分流。每个 pyrealiy 叶子
独占一个 _DnsTunnel pipeline（懒建），不同 outbound 之间不共享。

使用方法：将系统 DNS 改为 127.0.0.1:<port>，或用 iptables 把 53 端口重定向过来。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Optional

from .outbound import Outbound, PyrealiyOutbound
from .utils import get_logger, pack_address

logger = get_logger("dns")

_CN_DNS_DEFAULT     = "223.5.5.5"
_REMOTE_DNS_DEFAULT = "8.8.8.8"
_UDP_TIMEOUT        = 5.0
_TUNNEL_TIMEOUT     = 8.0


# ── DNS 报文工具 ───────────────────────────────────────────────────────────────

def _extract_domain(data: bytes) -> str | None:
    """从 DNS 查询报文中提取 QNAME（question 段不含指针压缩）"""
    try:
        offset = 12  # 跳过 12 字节固定头
        labels: list[str] = []
        while offset < len(data):
            n = data[offset]
            offset += 1
            if n == 0:
                break
            if n & 0xC0:  # 指针压缩不应出现在 question 段
                break
            labels.append(data[offset: offset + n].decode("ascii", errors="replace"))
            offset += n
        return ".".join(labels).lower() if labels else None
    except Exception:
        return None


def _question_end(data: bytes) -> int:
    """返回 question section 结束后的字节偏移；解析失败时回退到长度上限"""
    try:
        qdcount = int.from_bytes(data[4:6], "big")
        pos = 12
        for _ in range(qdcount):
            # QNAME：labels 直到 0x00（question 段不允许指针压缩）
            while pos < len(data):
                n = data[pos]
                if n == 0:
                    pos += 1
                    break
                if n & 0xC0:           # 防御性：偶遇指针就跳两字节
                    pos += 2
                    break
                pos += 1 + n
            pos += 4  # QTYPE(2) + QCLASS(2)
            if pos > len(data):
                return len(data)
        return pos
    except Exception:
        return len(data)


def _nxdomain(query: bytes) -> bytes:
    """保留原始 ID 与 question 段，置 QR=1 RCODE=3，清空 answer/authority/additional。

    必须只保留 question 段；如果带尾部 EDNS0/OPT 等 additional record，
    报头宣称 ARCOUNT=0 但附带数据会让严格 resolver 判 malformed。
    """
    if len(query) < 12:
        return query
    end = _question_end(query)
    h = bytearray(query[:12])
    h[2] = h[2] | 0x80                # QR=1，保留 Opcode/AA/TC/RD 等其他位
    h[3] = (h[3] & 0xF0) | 0x03       # RCODE=NXDOMAIN
    h[6] = h[7] = h[8] = h[9] = h[10] = h[11] = 0
    return bytes(h) + query[12:end]


# ── 查询后端 ───────────────────────────────────────────────────────────────────

async def _udp_query(data: bytes, host: str, port: int = 53) -> bytes:
    """向国内 DNS 发送 UDP 查询"""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[bytes] = loop.create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, d, _):
            if not fut.done():
                fut.set_result(d)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

        def connection_lost(self, _):
            if not fut.done():
                fut.set_exception(OSError("connection lost"))

    transport, _ = await loop.create_datagram_endpoint(_Proto, remote_addr=(host, port))
    try:
        transport.sendto(data)
        return await asyncio.wait_for(fut, timeout=_UDP_TIMEOUT)
    finally:
        transport.close()


class _DnsTunnel:
    """
    长寿命 DNS-over-TCP 隧道 + 并发 query pipeline。

    设计要点：
      - **单条** outbound 池连接持有；该 outbound 的所有 DNS 查询经它发出
      - 查询用我们自己的内部 tx_id（递增计数器）替换客户端原始 ID，保证唯一性，
        响应回来时再还原成客户端期望的 ID
      - 后台 reader 任务从隧道连续读 DNS-over-TCP 帧，按 tx_id 派发到 pending future
      - 并发上限 = MAX_INFLIGHT，超出时排队等待 slot（防止单 client 滥用拖垮 server）
      - 隧道掉线：reader 异常退出 → 所有 pending future 收到 OSError → 下次 query
        重试时惰性重建隧道；in-flight 查询都失败（端到端 resolver 会按其 timeout 重发）

    多 outbound 场景：每个 PyrealiyOutbound 单独持有一个 _DnsTunnel 实例。
    """

    MAX_INFLIGHT = 64

    def __init__(self, outbound: PyrealiyOutbound, remote_dns: str):
        self._outbound    = outbound
        self._remote_dns  = remote_dns
        self._ready       = None
        self._reader_task = None
        self._setup_lock  = asyncio.Lock()        # 只串行化"建立隧道"，不串行化 query
        self._inflight    = asyncio.Semaphore(self.MAX_INFLIGHT)
        # 内部 tx_id → (Future, 客户端原始 tx_id bytes)
        self._pending     = {}
        self._next_tx     = 1                     # 0 留给"未初始化"

    async def query(self, data: bytes) -> bytes:
        if len(data) < 2:
            raise ValueError("DNS query too short")

        async with self._inflight:
            for attempt in (0, 1):
                try:
                    await self._ensure_ready()
                    return await self._send_recv(data)
                except Exception:
                    self._drop_tunnel()
                    if attempt == 1:
                        raise

    async def _ensure_ready(self) -> None:
        if self._ready is not None and self._reader_task and not self._reader_task.done():
            return
        async with self._setup_lock:
            # 双重检查（其它 coroutine 可能在我们等锁期间已经建好）
            if self._ready is not None and self._reader_task and not self._reader_task.done():
                return
            ready = await self._outbound.acquire_tunnel()
            if ready is None:
                raise OSError(f"no tunnel available for DNS via '{self._outbound.tag}'")
            await ready.tunnel.send(pack_address(self._remote_dns, 53))
            self._ready       = ready
            self._reader_task = asyncio.create_task(self._reader_loop())

    def _allocate_tx(self) -> int:
        """
        分配一个不与 _pending 冲突的 tx_id（轮转 16-bit 空间，跳过 0）。

        MAX_INFLIGHT 信号量保证 _pending 远不会塞满 65535 id 空间，
        所以这个循环正常情况下第一次就返回。bounded loop 防御性：
        若将来 MAX_INFLIGHT 提升、或某次 finally 异常未清理，至少不会无声覆盖
        旧的 future 导致响应错配 —— 实在塞满就 raise 而不是死循环。
        """
        for _ in range(0x10000):
            tx_id = self._next_tx
            self._next_tx = (self._next_tx + 1) & 0xFFFF
            if self._next_tx == 0:
                self._next_tx = 1                   # 跳过 0
            if tx_id not in self._pending:
                return tx_id
        raise OSError("DNS pipeline tx_id space exhausted (pending overflow)")

    async def _send_recv(self, data: bytes) -> bytes:
        # 用内部 tx_id 替换客户端 ID，保证唯一（客户端 ID 可能重复或可预测）
        original_id = data[:2]
        tx_id       = self._allocate_tx()

        rewritten = tx_id.to_bytes(2, "big") + data[2:]
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        self._pending[tx_id] = (fut, original_id)
        try:
            await self._ready.tunnel.send(struct.pack("!H", len(rewritten)) + rewritten)
            resp = await asyncio.wait_for(fut, timeout=_TUNNEL_TIMEOUT)
            # 还原客户端原始 tx_id
            return original_id + resp[2:]
        finally:
            self._pending.pop(tx_id, None)

    async def _reader_loop(self) -> None:
        """
        持续从隧道读 DNS-over-TCP 帧（2 字节长度 + DNS 报文），
        按内部 tx_id 派发给对应 future。
        隧道结束 / 任何异常 → 唤醒所有 pending future 并退出。
        """
        buf = bytearray()
        try:
            while True:
                chunk = await self._ready.tunnel.recv()
                if not chunk:
                    raise OSError("DNS tunnel EOF")
                buf.extend(chunk)
                # 可能一次 recv 拿到多帧，全部处理掉
                while len(buf) >= 2:
                    frame_len = struct.unpack("!H", bytes(buf[:2]))[0]
                    if len(buf) < 2 + frame_len:
                        break
                    if frame_len < 2:
                        del buf[:2 + frame_len]
                        continue
                    resp     = bytes(buf[2: 2 + frame_len])
                    del buf[:2 + frame_len]
                    resp_tx  = int.from_bytes(resp[:2], "big")
                    entry    = self._pending.get(resp_tx)
                    if entry is not None and not entry[0].done():
                        entry[0].set_result(resp)
        except Exception as e:
            err = e if isinstance(e, Exception) else OSError(str(e))
            for fut, _ in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)

    def _drop_tunnel(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None
        if self._ready is not None:
            try:
                self._ready.close()
            except Exception:
                pass
            self._ready = None

    def close(self) -> None:
        self._drop_tunnel()


# ── asyncio 协议层 ─────────────────────────────────────────────────────────────

class _DNSProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self._handler   = handler
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data: bytes, addr):
        asyncio.create_task(self._dispatch(data, addr))

    async def _dispatch(self, data: bytes, addr):
        response = await self._handler(data)
        if response is not None and self._transport:
            self._transport.sendto(response, addr)


# ── 公共接口 ───────────────────────────────────────────────────────────────────

class DNSForwarder:
    def __init__(self, cfg: dict, router, outbounds: dict[str, Outbound]):
        self._host       = cfg.get("dns_listen_host", "127.0.0.1")
        self._port       = int(cfg.get("dns_listen_port", 5353))
        self._cn_dns     = cfg.get("cn_dns", _CN_DNS_DEFAULT)
        self._remote_dns = cfg.get("remote_dns", _REMOTE_DNS_DEFAULT)
        self._router     = router
        self._outbounds  = outbounds
        self._transport  = None
        # 每个 pyrealiy 叶子独占一条 DoT pipeline，懒建
        self._tunnels: dict[str, _DnsTunnel] = {}

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DNSProtocol(self._handle),
            local_addr=(self._host, self._port),
        )
        logger.info(
            "DNS forwarder on %s:%d  (direct->%s  proxy->%s via outbound's tunnel)",
            self._host, self._port, self._cn_dns, self._remote_dns,
        )

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
        for t in self._tunnels.values():
            t.close()
        self._tunnels.clear()

    def _tunnel_for(self, leaf: PyrealiyOutbound) -> _DnsTunnel:
        t = self._tunnels.get(leaf.tag)
        if t is None:
            t = _DnsTunnel(leaf, self._remote_dns)
            self._tunnels[leaf.tag] = t
        return t

    async def _handle(self, data: bytes) -> Optional[bytes]:
        domain = _extract_domain(data)
        if domain is None:
            return None

        action, source = self._router.match(domain)
        outbound = self._outbounds.get(action)
        if outbound is None:
            logger.warning("DNS router returned unknown outbound '%s' for %s [%s]",
                           action, domain, source)
            return _nxdomain(data)

        leaf = outbound.resolve_leaf()

        try:
            if leaf.type == "block":
                logger.debug("DNS block   %s  [%s]", domain, source)
                return _nxdomain(data)
            if leaf.type == "direct":
                logger.debug("DNS direct  %s -> %s  [%s]", domain, self._cn_dns, source)
                return await _udp_query(data, self._cn_dns)
            if isinstance(leaf, PyrealiyOutbound):
                logger.debug("DNS via %-12s %s -> %s  [%s]",
                             leaf.tag, domain, self._remote_dns, source)
                return await self._tunnel_for(leaf).query(data)
            # leaf 是组本身（所有 child 均 unhealthy 时 resolve_leaf 返回 self）
            # 或者未来扩展的未知 leaf 类型 —— 都视作"无可路由出口"，NXDOMAIN
            logger.warning("DNS '%s' [%s]: outbound '%s' has no healthy leaf, NXDOMAIN",
                           domain, source, outbound.tag)
            return _nxdomain(data)
        except Exception as e:
            logger.warning("DNS query failed for %s via %s: %s", domain, leaf.tag, e)
            return _nxdomain(data)
