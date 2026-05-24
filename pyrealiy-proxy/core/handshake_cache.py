"""
TLS 握手缓存（多份轮换 + 每日刷新）

【已知局限的根本原因】
ServerKeyExchange 签名 = sign(client_random ‖ server_random ‖ ECDHE参数)
替换 server_random → 签名失效 → 深度验证可检测

【缓解方案，无需私钥】
A. 不替换 server_random，改为预取多份握手（每份 random 各不相同）
   → 探测每次拿到不同的合法签名，重放检测需要"多次碰到同一份"才能发现
B. 每 24 小时自动从伪装站点重新取，历史 random 不会被二次观测
   → 两种手段叠加，实际暴露窗口极小

参数 POOL_SIZE 控制缓存数量，默认 8。
REFRESH_INTERVAL_SEC 控制刷新周期，默认 86400（24h）。
"""


from __future__ import annotations

import asyncio

import os

import random

import struct

import time


from .utils import get_logger

logger = get_logger("handshake_cache")

_TLS_HANDSHAKE     = 0x16
_TLS_CHANGE_CIPHER = 0x14
_HS_SERVER_HELLO_DONE = 0x0e

POOL_SIZE            = 8       # 同时持有的握手份数
REFRESH_INTERVAL_SEC = 86400   # 每 24 小时刷新一次


# ── 底层工具函数 ───────────────────────────────────────────────────────────────

async def _read_tls_record(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    ct = header[0]
    length = int.from_bytes(header[3:5], "big")
    body = await reader.readexactly(length)
    return ct, header + body


def _build_tls12_client_hello(server_name: str) -> bytes:
    """
    构造只允许 TLS 1.2 的 ClientHello（无 supported_versions 扩展）。
    TLS 1.2 的 Certificate 明文传输，适合缓存回放。
    """
    rnd  = os.urandom(32)
    sni  = server_name.encode()

    sni_entry = struct.pack("!BH", 0, len(sni)) + sni
    sni_list  = struct.pack("!H", len(sni_entry)) + sni_entry
    ext_sni   = struct.pack("!HH", 0x0000, len(sni_list)) + sni_list

    groups      = struct.pack("!HH", 0x001d, 0x0017)
    groups_list = struct.pack("!H", len(groups)) + groups
    ext_groups  = struct.pack("!HH", 0x000a, len(groups_list)) + groups_list

    ext_ecpf = struct.pack("!HHBB", 0x000b, 2, 1, 0x00)

    sig_algs = struct.pack("!8H",
        0x0403, 0x0804, 0x0401,
        0x0503, 0x0805, 0x0501,
        0x0806, 0x0601,
    )
    sa_list = struct.pack("!H", len(sig_algs)) + sig_algs
    ext_sa  = struct.pack("!HH", 0x000d, len(sa_list)) + sa_list

    extensions    = ext_sni + ext_groups + ext_ecpf + ext_sa
    cipher_suites = struct.pack("!7H",
        0xC02B, 0xC02F, 0xC02C, 0xC030, 0xC013, 0xC014, 0x00FF,
    )

    hello_body = (
        b"\x03\x03" + rnd + b"\x00"
        + struct.pack("!H", len(cipher_suites)) + cipher_suites
        + b"\x01\x00"
        + struct.pack("!H", len(extensions)) + extensions
    )
    handshake = b"\x01" + struct.pack("!I", len(hello_body))[1:] + hello_body
    return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake


async def _fetch_one(host: str, port: int) -> list[bytes]:
    """
    与伪装站点完成一次 TLS 1.2 握手，返回服务端的所有握手记录。
    返回空列表表示失败。
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10.0
        )
    except Exception as e:
        logger.debug("_fetch_one connect error: %s", e)
        return []

    records: list[bytes] = []
    try:
        writer.write(_build_tls12_client_hello(host))
        await writer.drain()

        while True:
            ct, raw = await asyncio.wait_for(_read_tls_record(reader), timeout=8.0)
            records.append(raw)

            if ct == _TLS_HANDSHAKE and len(raw) > 5 and raw[5] == _HS_SERVER_HELLO_DONE:
                break  # 拿到 ServerHelloDone，握手记录完整
            if ct == _TLS_CHANGE_CIPHER:
                logger.warning("Server responded TLS 1.3 — certificate is encrypted, cache will be incomplete")
                break

    except asyncio.TimeoutError:
        pass
    except Exception as e:
        logger.debug("_fetch_one record error: %s", e)
        records = []
    finally:
        writer.close()

    return records


# ── 多份握手缓存池 ────────────────────────────────────────────────────────────

class HandshakeCache:
    """
    维护一个大小为 POOL_SIZE 的握手记录池，每次探测随机取一份回放，
    并每 24 小时自动从伪装站点刷新所有缓存。

    签名有效性保证：
      不修改 server_random → ServerKeyExchange 签名全程有效。
      重放检测概率 = 1/POOL_SIZE（默认 1/8），且每日换新。
    """

    def __init__(self, host: str, port: int = 443):
        self._host = host
        self._port = port
        self._pool: list[list[bytes]] = []   # 每个元素是一份完整的握手记录列表
        self._last_refresh = 0.0

    async def warmup(self) -> bool:
        """启动时填满缓存池，并启动后台定时刷新任务。"""
        ok = await self._fill_pool()
        if ok:
            asyncio.create_task(self._auto_refresh_loop())
        return ok

    async def _fill_pool(self) -> bool:
        logger.info("Fetching %d TLS sessions from %s ...", POOL_SIZE, self._host)
        pool: list[list[bytes]] = []

        # 并发拉取，加快启动速度
        tasks = [_fetch_one(self._host, self._port) for _ in range(POOL_SIZE)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, list) and r:
                pool.append(r)

        if not pool:
            logger.error("Failed to fetch any TLS session from %s", self._host)
            return False

        self._pool = pool
        self._last_refresh = time.monotonic()
        logger.info("Cached %d/%d TLS sessions from %s", len(pool), POOL_SIZE, self._host)
        return True

    async def _auto_refresh_loop(self) -> None:
        """每 24 小时在后台静默刷新缓存池，不中断正在进行的请求。"""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
            logger.info("Refreshing handshake cache ...")
            new_pool: list[list[bytes]] = []
            tasks = [_fetch_one(self._host, self._port) for _ in range(POOL_SIZE)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list) and r:
                    new_pool.append(r)
            if new_pool:
                self._pool = new_pool      # 原子替换，不影响并发读
                self._last_refresh = time.monotonic()
                logger.info("Cache refreshed: %d sessions", len(new_pool))
            else:
                logger.warning("Cache refresh failed, keeping old sessions")

    @property
    def ready(self) -> bool:
        return bool(self._pool)

    async def send_server_hello_done(
        self,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """
        向合法客户端发送缓存的服务端握手记录（ServerHello…ServerHelloDone）。
        不关闭连接，调用方继续读取客户端的 CKE+CCS+Finished 并完成握手模拟。
        返回 False 表示缓存为空（调用方应关闭连接）。
        """
        if not self._pool:
            return False
        session_records = random.choice(self._pool)
        try:
            for raw in session_records:
                writer.write(raw)
            await writer.drain()
            return True
        except Exception:
            return False

    async def serve_probe(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """
        从池中随机选一份握手记录回放给探测连接。
        server_random 保持原样 → ServerKeyExchange 签名有效。
        """
        if not self._pool:
            client_writer.close()
            return

        session_records = random.choice(self._pool)
        try:
            for raw in session_records:
                client_writer.write(raw)
            await client_writer.drain()

            # 等探测器的 ClientKeyExchange/Finished，之后自然断开
            await asyncio.wait_for(client_reader.read(4096), timeout=2.0)
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass
