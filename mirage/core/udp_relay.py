"""
UDP-over-TCP 转发：SOCKS5 UDP ASSOCIATE + 加密隧道 UDP 帧封装

═══ 协议设计 ═══

整体架构：

    App ──UDP──► Client SOCKS5 UDP relay ──TCP(加密)──► Server ──UDP──► Target
                       ↕                                    ↕
                 wraps datagrams                    unwraps + sends

客户端流：
  1. SOCKS5 client 发 UDP ASSOCIATE (CMD=0x03) 到 TCP 控制连接
  2. 客户端 acquire 一条 tunnel，向其发首包 b"\\x00"（host_len=0 哨兵 → UDP 模式）
  3. 客户端绑本地 UDP socket，回 BND.ADDR:BND.PORT 给 SOCKS5 client
  4. 之后 SOCKS5 client 发 UDP 包含 SOCKS5 UDP header（RSV/FRAG/ATYP/...）
  5. 客户端解包 SOCKS5 header，重新封装为 tunnel 帧 [2B len][packed_addr][payload]
     送进 tunnel
  6. tunnel.recv 取出反向帧，重建 SOCKS5 UDP header，sendto 给 SOCKS5 client
  7. TCP 控制连接关 / UDP idle > timeout → 结束 relay

服务端流：
  1. 收到客户端 tunnel 的第一条加密 record，是 b"\\x00" → 进 UDP 模式
  2. 创建一个共享 UDP socket（用 OS ephemeral port）
  3. tunnel.recv → 解帧 → sendto(target, payload)
  4. socket.recvfrom(target_reply) → 封帧 [packed_target_addr][payload] → tunnel.send

═══ 兼容性 ═══

  - 老 TCP 客户端发 host_len > 0 的 pack_address 作为首包，server 看到首字节
    非 0、走老 TCP 路径，**完全兼容**
  - UDP 模式哨兵 b"\\x00" 是单字节，避开了任何合法 host_len（最短 IP 字符串
    "0.0.0.0" host_len=7）

═══ 帧格式 ═══

  在加密隧道内（每个 UDP 报文一帧）：
    [2B 总长度 BE][packed_addr][UDP payload]
    packed_addr = [1B host_len][host bytes][2B port]
    总长度 = len(packed_addr) + len(payload)

  注：tunnel.send 把数据切成 record（4/8/16KB 桶），单个大 UDP 帧可能跨多条
  record；FrameReader 做缓冲式解帧。

═══ 路由 ═══

  UDP relay 绑定到 SOCKS5 控制连接所在 outbound（默认 route.final）。多个
  UDP 目标共用同一 tunnel（option B）。后续若要按 UDP 目标分流，需在 frame
  级别做路由 —— 不在本版。

═══ 限制 ═══

  - SOCKS5 UDP FRAG != 0（分片）不支持，直接丢
  - 单包硬上限 _UDP_MAX_PACKET 65507 字节
  - direct outbound 的 UDP 走本地 socket 直发（不经隧道）
  - block / 其他类型 outbound 拒绝 UDP ASSOCIATE
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from typing import Iterator, Optional

from .utils import get_logger, pack_address, unpack_address, is_ip_literal

logger = get_logger("udp_relay")

_UDP_MAX_PACKET    = 65507  # IPv4 UDP payload 上限
_UDP_IDLE_TIMEOUT  = 60.0   # 无流量秒数后关闭 relay
_UDP_QUEUE_MAXSIZE = 1024   # 上行队列上限：满则丢包（UDP 本就 best-effort，背压更友好）


def _normalize_addr_for_dualstack(host: str, port: int) -> tuple[str, int]:
    """
    把 IPv4 字面量映射到 IPv6 v4-mapped 形式，让 dual-stack v6 socket 也能发。

    域名 / v6 / 已是 mapped 直接返回（让 OS 处理）。仅纯 IPv4 字符串映射。
    """
    if ":" in host:
        return (host, port)
    try:
        socket.inet_aton(host)
        return (f"::ffff:{host}", port)
    except OSError:
        return (host, port)   # 域名或非法


def _unmap_v4(host: str) -> str:
    """v4-mapped IPv6（::ffff:1.2.3.4）还原为 v4 字面（1.2.3.4）；其它原样返回。"""
    try:
        import ipaddress
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
    except (ValueError, ImportError):
        pass
    return host


async def _create_dualstack_udp_endpoint(loop: asyncio.AbstractEventLoop,
                                        protocol_factory):
    """
    建一个 IPv6 dual-stack UDP socket（同时收发 v4/v6），失败时回落到纯 IPv4。

    Linux 默认 IPV6_V6ONLY=0 → 一个 v6 socket 收发两栈。Windows / 老 BSD 可能
    需要显式设置。失败时降级到 ("0.0.0.0", 0) v4 socket。
    """
    try:
        return await loop.create_datagram_endpoint(
            protocol_factory, local_addr=("::", 0),
            family=socket.AF_INET6, reuse_port=False,
        )
    except Exception as e:
        logger.debug("dual-stack v6 UDP socket failed (%s), falling back to v4", e)
        return await loop.create_datagram_endpoint(
            protocol_factory, local_addr=("0.0.0.0", 0),
            family=socket.AF_INET,
        )


# ── 加密隧道内的 UDP 帧封装 ───────────────────────────────────────────────────

def pack_udp_frame(target_host: str, target_port: int, payload: bytes) -> bytes:
    """
    封装一个 UDP 帧用于在加密隧道内传输。

      [2B 总长度 BE][packed_addr][payload]
      packed_addr = pack_address(host, port) = [1B host_len][host bytes][2B port]

    总长度限制 = 65535（uint16）；UDP payload 最大 65507，加上 packed_addr
    几十字节，远在 uint16 之内。
    """
    addr = pack_address(target_host, target_port)
    body = addr + payload
    return struct.pack("!H", len(body)) + body


class FrameReader:
    """
    从 tunnel.recv() 的不定长 chunks 里拼出完整 UDP 帧。

    tunnel.send 切 record（4/8/16KB 桶），大的 UDP 帧可能跨多条 record；本类
    用 buffer 累积、按帧头 length 切出完整帧。
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    def frames(self) -> Iterator[tuple[str, int, bytes]]:
        """生成 (host, port, payload) 元组，buffer 不够一帧时停"""
        while len(self._buf) >= 2:
            total = struct.unpack("!H", bytes(self._buf[:2]))[0]
            if len(self._buf) < 2 + total:
                return
            body = bytes(self._buf[2: 2 + total])
            del self._buf[:2 + total]
            try:
                host, port, consumed = unpack_address(body)
                yield host, port, body[consumed:]
            except Exception as e:
                logger.debug("frame parse failed: %s", e)
                continue


# ── SOCKS5 UDP 头解析（RFC 1928 §7）─────────────────────────────────────────

# 格式：
#   [2B RSV][1B FRAG][1B ATYP][addr...][2B port][payload]
#   ATYP: 0x01=IPv4, 0x03=domain, 0x04=IPv6

def parse_socks5_udp_header(data: bytes) -> Optional[tuple[str, int, bytes]]:
    """返回 (target_host, target_port, payload)；非法返回 None"""
    if len(data) < 6 or data[0] != 0 or data[1] != 0:
        return None
    if data[2] != 0:
        return None  # FRAG != 0：分片包不支持
    atyp = data[3]
    pos = 4
    if atyp == 0x01:
        if len(data) < pos + 4 + 2:
            return None
        host = socket.inet_ntoa(data[pos: pos + 4])
        pos += 4
    elif atyp == 0x03:
        if len(data) < pos + 1:
            return None
        dlen = data[pos]; pos += 1
        if len(data) < pos + dlen + 2:
            return None
        host = data[pos: pos + dlen].decode("ascii", errors="replace")
        pos += dlen
    elif atyp == 0x04:
        if len(data) < pos + 16 + 2:
            return None
        host = socket.inet_ntop(socket.AF_INET6, data[pos: pos + 16])
        pos += 16
    else:
        return None
    port = struct.unpack("!H", data[pos: pos + 2])[0]
    pos += 2
    return host, port, data[pos:]


def build_socks5_udp_header(host: str, port: int) -> bytes:
    """构造 SOCKS5 UDP 头用于回包（不含 payload）"""
    out = b"\x00\x00\x00"   # RSV(2) + FRAG=0
    try:
        addr = socket.inet_aton(host)
        out += b"\x01" + addr
    except OSError:
        try:
            addr6 = socket.inet_pton(socket.AF_INET6, host)
            out += b"\x04" + addr6
        except OSError:
            hb = host.encode("ascii", errors="replace")
            out += b"\x03" + bytes([len(hb) & 0xff]) + hb
    out += struct.pack("!H", port)
    return out


# ── 客户端 UDPRelay ───────────────────────────────────────────────────────────

class UDPRelay:
    """
    单个 SOCKS5 UDP ASSOCIATE 会话的客户端侧 relay。

    生命周期绑定到 SOCKS5 TCP 控制连接：
      - control_reader EOF → 立即关 relay（RFC 1928 §7 要求）
      - UDP 无流量 idle_timeout 秒 → 关
      - tunnel 异常 → 关

    路由：tunnel != None 走加密隧道；tunnel == None 走本地直连 UDP（direct
    outbound）。
    """

    def __init__(self,
                 control_reader: asyncio.StreamReader,
                 control_writer: asyncio.StreamWriter,
                 tunnel,                 # core.tunnel.EncryptedTunnel 或 None
                 server_writer,          # tunnel 的底层 writer（用于清理）
                 bind_host: str,
                 idle_timeout: float = _UDP_IDLE_TIMEOUT,
                 on_up=None,
                 on_down=None) -> None:
        self._ctrl_r       = control_reader
        self._ctrl_w       = control_writer
        self._tunnel       = tunnel
        self._server_w     = server_writer
        self._bind_host    = bind_host
        self._idle_timeout = idle_timeout
        self._on_up        = on_up
        self._on_down      = on_down

        # 本地 SOCKS5 入口的 transport（接 SOCKS5 client 的 UDP）
        self._local_transport: Optional[asyncio.DatagramTransport] = None
        # direct 模式下额外的"出口"transport（同时也是回包入口）
        self._direct_transport: Optional[asyncio.DatagramTransport] = None

        # SOCKS5 client 最近一次发包的源 addr（用于回包）。每次有上行包到都
        # 刷新——支持多源端口场景（DNS 每查询新源端口），避免回包发错口
        self._socks_client_addr: Optional[tuple] = None

        # 上行队列：datagram_received 同步 put_nowait，单消费协程串行处理。
        # 取代"每包一个 Task"模式，省 GC 压、自带背压（满则丢，UDP 本就 best-effort）
        self._uplink_q: asyncio.Queue = asyncio.Queue(maxsize=_UDP_QUEUE_MAXSIZE)

        self._last_activity = time.monotonic()
        self._stop = asyncio.Event()

    async def start(self) -> tuple[str, int]:
        """绑本地 UDP socket（SOCKS5 入口），返回 (bnd_host, bnd_port)"""
        loop = asyncio.get_event_loop()
        relay = self

        class _LocalProto(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                pass
            def datagram_received(self, data, addr):
                # 同步 put：满则丢（UDP best-effort 背压策略）
                try:
                    relay._uplink_q.put_nowait((data, addr))
                except asyncio.QueueFull:
                    pass
            def error_received(self, exc):
                logger.debug("local UDP error: %s", exc)

        self._local_transport, _ = await loop.create_datagram_endpoint(
            _LocalProto, local_addr=(self._bind_host, 0)
        )
        bnd = self._local_transport.get_extra_info("sockname")
        return bnd[0], bnd[1]

    async def run(self) -> None:
        """阻塞跑到 stop 触发；负责清理 transport / tunnel"""
        loop = asyncio.get_event_loop()
        relay = self

        # 先把 direct 模式的 transport 建好，再启 consumer task —— 否则
        # consumer 可能在 await create_endpoint 期间被调度跑到 _handle_uplink，
        # 访问 None 的 _direct_transport 导致 race
        if self._tunnel is None:
            class _DirectProto(asyncio.DatagramProtocol):
                def connection_made(self, transport):
                    pass
                def datagram_received(self, data, addr):
                    relay._on_direct_reply(addr, data)
                def error_received(self, exc):
                    logger.debug("direct UDP error: %s", exc)
            self._direct_transport, _ = await _create_dualstack_udp_endpoint(loop, _DirectProto)

        tasks = [asyncio.create_task(self._uplink_consumer()),
                 asyncio.create_task(self._control_watcher()),
                 asyncio.create_task(self._idle_watcher())]

        if self._tunnel is not None:
            tasks.append(asyncio.create_task(self._tunnel_to_local()))

        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
                    try: await t
                    except asyncio.CancelledError: pass
            self._cleanup()

    # ── 上行 (SOCKS5 client → 远端) ────────────────────────────────────────
    async def _uplink_consumer(self) -> None:
        """串行消费 _uplink_q —— 替代旧的"每包一个 Task"模式"""
        while True:
            try:
                data, addr = await self._uplink_q.get()
            except Exception:
                return
            try:
                await self._handle_uplink(data, addr)
            except Exception as e:
                # 守门：parse 异常 / 其它意外都吃掉，避免变成 task unhandled exception
                logger.debug("uplink packet error: %s", e)

    async def _handle_uplink(self, data: bytes, addr) -> None:
        # 每包都刷新 SOCKS5 client addr，支持多源端口（DNS 每查询新口）
        self._socks_client_addr = addr

        if len(data) > _UDP_MAX_PACKET:
            return

        parsed = parse_socks5_udp_header(data)
        if parsed is None:
            return  # FRAG != 0 或非法头
        target_host, target_port, payload = parsed
        self._last_activity = time.monotonic()

        if self._tunnel is not None:
            try:
                await self._tunnel.send(pack_udp_frame(target_host, target_port, payload))
                if self._on_up:
                    self._on_up(len(payload))
            except Exception as e:
                logger.debug("tunnel send failed: %s", e)
                self._stop.set()
        else:
            # direct：dual-stack v6 socket 出，IPv4 目标自动 mapped。
            # **不接受域名目标**——Python socket.sendto 对域名会阻塞调内核
            # getaddrinfo，**冻结整个 asyncio 事件循环**。彻底异步 UDP DNS 解析
            # 留待 v2（多数 UDP 应用直接用 IP 作 target，不踩到这条路径）
            if not is_ip_literal(target_host):
                logger.debug("direct UDP target %s is not an IP literal, dropping "
                             "(domain UDP target needs async DNS, v2 work)",
                             target_host)
                return
            try:
                self._direct_transport.sendto(
                    payload,
                    _normalize_addr_for_dualstack(target_host, target_port),
                )
                if self._on_up:
                    self._on_up(len(payload))
            except Exception as e:
                logger.debug("direct sendto %s:%d failed: %s",
                             target_host, target_port, e)

    # ── 下行 (远端 → SOCKS5 client) ────────────────────────────────────────
    async def _tunnel_to_local(self) -> None:
        reader = FrameReader()
        try:
            while True:
                chunk = await self._tunnel.recv()
                if not chunk:
                    break
                reader.feed(chunk)
                for src_host, src_port, payload in reader.frames():
                    self._last_activity = time.monotonic()
                    self._send_to_socks_client(src_host, src_port, payload)
        except Exception as e:
            logger.debug("tunnel recv ended: %s", e)
        finally:
            self._stop.set()

    def _on_direct_reply(self, addr, data: bytes) -> None:
        """direct 模式下 v6 socket 收到回包（同步回调）"""
        host = _unmap_v4(addr[0])
        port = addr[1]
        self._last_activity = time.monotonic()
        self._send_to_socks_client(host, port, data)

    def _send_to_socks_client(self, src_host: str, src_port: int, payload: bytes) -> None:
        if self._socks_client_addr is None or self._local_transport is None:
            return
        try:
            self._local_transport.sendto(
                build_socks5_udp_header(src_host, src_port) + payload,
                self._socks_client_addr,
            )
            if self._on_down:
                self._on_down(len(payload))
        except Exception as e:
            logger.debug("local sendto SOCKS5 client failed: %s", e)

    # ── 看门狗 ──────────────────────────────────────────────────────────────
    async def _control_watcher(self) -> None:
        """SOCKS5 TCP 控制连接关 → 立即停 relay（RFC 1928 §7）"""
        try:
            while True:
                data = await self._ctrl_r.read(1024)
                if not data:
                    break
        except Exception:
            pass
        self._stop.set()

    async def _idle_watcher(self) -> None:
        check_interval = max(5.0, self._idle_timeout / 4)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=check_interval)
                return
            except asyncio.TimeoutError:
                if time.monotonic() - self._last_activity > self._idle_timeout:
                    logger.debug("UDP relay idle timeout (%.0fs)", self._idle_timeout)
                    self._stop.set()
                    return

    def _cleanup(self) -> None:
        if self._local_transport is not None:
            try: self._local_transport.close()
            except Exception: pass
            self._local_transport = None
        if self._direct_transport is not None:
            try: self._direct_transport.close()
            except Exception: pass
            self._direct_transport = None
        # tunnel 的关由调用方（client.py）处理


# ── 服务端 UDP-mode tunnel 处理 ───────────────────────────────────────────────

async def handle_udp_tunnel(tunnel,
                            on_byte_in=None,
                            on_byte_out=None,
                            idle_timeout: float = 600.0) -> None:
    """
    服务端：处理已进入 UDP 模式的 tunnel（客户端发了 b"\\x00" 哨兵首包之后）。

    桥接：tunnel ↔ 一个 **dual-stack v6 UDP socket**（同时支持 v4/v6 target）。

    下行回包用队列消费：datagram_received 同步 put_nowait + 单个消费协程串行
    `tunnel.send`，避免每包一个 Task 的 GC / 调度开销；队列满则丢（UDP 本就
    best-effort）。

    域名目标：直接丢弃。Python `socket.sendto` 对域名会调内核 getaddrinfo
    阻塞事件循环（详见 UDPRelay._handle_uplink 对应注释）。

    idle_timeout: 双向都 idle 这么久后关 tunnel（释放 UDP socket + tunnel TCP
                  连接）。默认 600s。tunnel TCP 死链路通常 TCP keepalive ~90s 内
                  会触发，但 idle 兜底更早回收。

    on_byte_in / on_byte_out：可选的字节计数回调。
    """
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    downlink_q: asyncio.Queue = asyncio.Queue(maxsize=_UDP_QUEUE_MAXSIZE)
    last_activity = time.monotonic()

    def _touch() -> None:
        nonlocal last_activity
        last_activity = time.monotonic()

    class _Proto(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            pass
        def datagram_received(self, data, addr):
            try:
                downlink_q.put_nowait((data, addr))
            except asyncio.QueueFull:
                pass
        def error_received(self, exc):
            logger.debug("[udp-srv] socket error: %s", exc)

    transport, _ = await _create_dualstack_udp_endpoint(loop, _Proto)

    async def _downlink_consumer():
        """从队列串行取出 target 回包，封帧送回 tunnel"""
        while True:
            try:
                data, addr = await downlink_q.get()
            except Exception:
                return
            _touch()
            host = _unmap_v4(addr[0])
            port = addr[1]
            try:
                await tunnel.send(pack_udp_frame(host, port, data))
            except Exception as e:
                logger.debug("[udp-srv] tunnel send failed: %s", e)
                stop.set()
                return
            if on_byte_out is not None:
                on_byte_out(len(data))

    async def _tunnel_to_target():
        reader = FrameReader()
        try:
            while True:
                chunk = await tunnel.recv()
                if not chunk:
                    break
                _touch()
                reader.feed(chunk)
                for tgt_host, tgt_port, payload in reader.frames():
                    if len(payload) > _UDP_MAX_PACKET:
                        continue
                    if not is_ip_literal(tgt_host):
                        logger.debug("[udp-srv] domain target %s dropped "
                                     "(v2 needs async DNS)", tgt_host)
                        continue
                    try:
                        transport.sendto(
                            payload,
                            _normalize_addr_for_dualstack(tgt_host, tgt_port),
                        )
                    except Exception as e:
                        logger.debug("[udp-srv] sendto %s:%d failed: %s",
                                     tgt_host, tgt_port, e)
                        continue
                    if on_byte_in is not None:
                        on_byte_in(len(payload))
        except Exception as e:
            logger.debug("[udp-srv] tunnel recv ended: %s", e)
        finally:
            stop.set()

    async def _idle_watcher():
        check_interval = max(5.0, idle_timeout / 4)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=check_interval)
                return
            except asyncio.TimeoutError:
                if time.monotonic() - last_activity > idle_timeout:
                    logger.debug("[udp-srv] idle timeout (%.0fs)", idle_timeout)
                    stop.set()
                    return

    tasks = [asyncio.create_task(_tunnel_to_target()),
             asyncio.create_task(_downlink_consumer()),
             asyncio.create_task(_idle_watcher())]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
                try: await t
                except asyncio.CancelledError: pass
        transport.close()
