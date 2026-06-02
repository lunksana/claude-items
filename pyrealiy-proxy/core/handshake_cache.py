"""
TLS 握手缓存（多份轮换 + 每小时刷新）

【设计决策：使用 TLS 1.3 抓取】
客户端发送 TLS 1.3-capable ClientHello（含 supported_versions 扩展）。
如果服务端缓存使用 TLS 1.2-only ClientHello 去抓 apple.com，
apple.com 会返回 TLS 1.2，但我们的客户端声称支持 TLS 1.3——
版本降级本身就是一个可被 GFW 利用的强指纹。

修正方案：缓存抓取时使用与真实客户端相同规格的 TLS 1.3 ClientHello，
apple.com 返回 TLS 1.3：ServerHello + CCS(compat) + 加密记录群（EncryptedExtensions
+ Certificate + CertificateVerify + Finished，均为 0x17 记录）。
这些加密记录对 GFW 是不透明的，无法读取证书内容，只能看到合法的 TLS 1.3 协商。

【重放检测缓解】
POOL_SIZE = 32：碰撞期望探针数从 ~4 提升至 ~8
REFRESH_INTERVAL_SEC = 3600：每小时换新，缩短历史记录被观测的时间窗口
"""

from __future__ import annotations

import asyncio
import os
import random
import time

from .tls_raw import build_client_hello
from .utils import get_logger

logger = get_logger("handshake_cache")

_TLS_CHANGE_CIPHER = 0x14   # 唯一被代码逻辑用到的常量（ServerHello 0x16 等仅出现于注释）

POOL_SIZE            = 32    # 同时持有的握手份数（扩大以降低重放碰撞概率）
REFRESH_INTERVAL_SEC = 3600  # 每小时刷新一次（缩短历史 random 暴露窗口）


# ── 底层工具函数 ───────────────────────────────────────────────────────────────

async def _read_tls_record(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    ct     = header[0]
    length = int.from_bytes(header[3:5], "big")
    body   = await reader.readexactly(length)
    return ct, header + body


async def _fetch_one(host: str, port: int) -> list[bytes]:
    """
    与伪装站点完成一次 TLS 1.3 握手，返回服务端的完整握手记录列表。

    使用与真实客户端相同规格的 TLS 1.3-capable ClientHello，
    保证抓取到的是 TLS 1.3 响应，与客户端发出的 ClientHello 版本一致。

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
        # 使用与真实客户端相同的 TLS 1.3-capable ClientHello（随机 session_id）
        hello, _ = build_client_hello(host, os.urandom(32))
        writer.write(hello)
        await writer.drain()

        saw_ccs   = False
        enc_count = 0

        while True:
            # CCS 之后切换短超时：服务端会快速发完整个 flight，超时即代表 flight 结束
            timeout = 2.0 if saw_ccs else 8.0
            try:
                ct, raw = await asyncio.wait_for(_read_tls_record(reader), timeout=timeout)
            except asyncio.TimeoutError:
                break  # server flight done

            records.append(raw)

            if ct == _TLS_CHANGE_CIPHER:
                saw_ccs = True
            elif ct == 0x17 and saw_ccs:
                # EncryptedExtensions + Certificate + CertificateVerify + Finished
                enc_count += 1
                if enc_count >= 6:   # 余量：通常 4 条，最多 6 条
                    break

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
    并每小时自动从伪装站点刷新所有缓存。

    缓存的是真实 apple.com 的 TLS 1.3 握手记录：
      ServerHello(0x16) + CCS(0x14) + 加密记录群(0x17...)
    加密记录对外不透明（包含证书，但 GFW 无法读取）。
    """

    def __init__(self, host: str, port: int = 443):
        self._host = host
        self._port = port
        self._pool: list[list[bytes]] = []
        self._last_refresh = 0.0
        self._refresh_task: asyncio.Task | None = None

    async def warmup(self) -> bool:
        """启动时填满缓存池，并启动后台定时刷新任务。"""
        ok = await self._fill_pool()
        if ok:
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        return ok

    async def _fill_pool(self) -> bool:
        logger.info("Fetching %d TLS 1.3 sessions from %s ...", POOL_SIZE, self._host)
        tasks   = [_fetch_one(self._host, self._port) for _ in range(POOL_SIZE)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pool: list[list[bytes]] = [
            r for r in results if isinstance(r, list) and r
        ]

        if not pool:
            logger.error("Failed to fetch any TLS session from %s", self._host)
            return False

        self._pool         = pool
        self._last_refresh = time.monotonic()
        logger.info("Cached %d/%d TLS sessions from %s", len(pool), POOL_SIZE, self._host)
        return True

    async def _auto_refresh_loop(self) -> None:
        """每小时在后台静默刷新缓存池，不中断正在进行的请求。"""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
            logger.info("Refreshing handshake cache ...")
            tasks   = [_fetch_one(self._host, self._port) for _ in range(POOL_SIZE)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            new_pool: list[list[bytes]] = [
                r for r in results if isinstance(r, list) and r
            ]
            if new_pool:
                self._pool         = new_pool
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
        向合法客户端发送缓存的服务端 TLS 1.3 握手记录（ServerHello + CCS + 加密记录群）。
        不关闭连接，调用方继续读取客户端的 CCS+Finished 并完成握手模拟。
        返回 False 表示缓存为空。
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
        GFW 探测器收到真实 apple.com 的 TLS 1.3 握手记录，
        无法在不掌握会话密钥的情况下验证或继续握手。
        """
        if not self._pool:
            client_writer.close()
            return

        session_records = random.choice(self._pool)
        try:
            for raw in session_records:
                client_writer.write(raw)
            await client_writer.drain()

            # 等探测器尝试继续（发送 CCS+Finished），之后自然断开
            await asyncio.wait_for(client_reader.read(4096), timeout=2.0)
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass
