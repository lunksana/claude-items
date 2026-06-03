"""工具函数：日志、字节操作等"""


from __future__ import annotations

import logging

import socket

import struct

import asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── asyncio 噪音抑制 ──────────────────────────────────────────────────────────

def install_stale_gaierror_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    asyncio 默认会把"Future exception was never retrieved"打 ERROR。
    pool 的 wait_for 超时取消任务时，asyncio 在线程池里跑的 getaddrinfo
    future 不可取消，等它真把 gaierror 报回来时已经没人 await 那个 future 了——
    这是 stale future 的副作用，不是真正的错误。降级到 DEBUG 避免吓人。

    client 和 server 都该装：server 处理 client 发来的域名 target 时
    同样可能在 open_connection 内部产生 stale getaddrinfo future。
    """
    _logger = get_logger("asyncio")

    def _handler(_loop, context):
        exc = context.get("exception")
        if isinstance(exc, socket.gaierror):
            _logger.debug("stale getaddrinfo future (ignored): %s", exc)
            return
        _loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def pack_address(host: str, port: int) -> bytes:
    """将目标地址打包为 [1字节类型][地址][2字节端口]"""
    host_bytes = host.encode()
    return struct.pack("!B", len(host_bytes)) + host_bytes + struct.pack("!H", port)


def unpack_address(data: bytes) -> tuple[str, int, int]:
    """解包地址，返回 (host, port, 消耗的字节数)"""
    host_len = data[0]
    host = data[1 : 1 + host_len].decode()
    port = struct.unpack("!H", data[1 + host_len : 3 + host_len])[0]
    consumed = 3 + host_len
    return host, port, consumed


_RELAY_BUF       = 32768      # 单次读取大小，平衡延迟与吞吐
_DRAIN_THRESHOLD = 64 * 1024  # 写缓冲积压超过此值才 drain，避免每帧切换协程
_CLOSE_TIMEOUT   = 2.0        # wait_closed 上限：避免对端不发 FIN 时永久挂起

# 中继协程的可选 debug 日志（默认静默；用户开启 logger("utils") 的 DEBUG 即可看到）
_RELAY_LOGGER = get_logger("utils")


def set_drain_threshold(n: int) -> None:
    """
    运行时调整 drain 阈值（relay + EncryptedTunnel.send 共用）。
    跨境长肥管道（高 BDP）调到 256KB~1MB 能让 pipeline 填满，吞吐更稳。
    代价：单条连接内存上升、bufferbloat 可能恶化延迟（短包场景反而变差）。
    """
    global _DRAIN_THRESHOLD
    _DRAIN_THRESHOLD = int(n)
    # 同步给 tunnel 模块用的同名常量
    try:
        from . import tunnel as _t
        _t._DRAIN_THRESHOLD = int(n)
    except ImportError:
        pass


def get_drain_threshold() -> int:
    """供 client/server 在 inline drain 检查里取当前阈值（set_drain_threshold 之后变化跟得上）"""
    return _DRAIN_THRESHOLD


def apply_log_levels(cfg: dict) -> None:
    """
    根据 cfg["log_levels"] 调整指定 logger 的级别，供配置文件控制调试范围。

    cfg 中无 "log_levels" 字段时 no-op，保留 basicConfig 的 INFO 默认。

    格式示例（写在 config_client.json / config_server.json 顶层）：

        "log_levels": {
            "default":  "INFO",      // 可选：root logger 级别
            "outbound": "DEBUG",     // 启用 outbound 模块的中继 leg 异常 + group 切换日志
            "server":   "DEBUG",     // 启用 server 模块的中继 leg 异常
            "utils":    "DEBUG",     // 启用 DirectOutbound 直连 leg 异常
            "router":   "DEBUG",     // 每条规则命中详情
            "dns":      "DEBUG"      // DNS 转发逐条决策
        }

    可用 logger 名（与 get_logger() 一一对应）：
      client / server / outbound / group / healthcheck / conn_pool / router /
      dns / camouflage / handshake_cache / geo_cache / utils / socks5 /
      tproxy / brutal / egress / asyncio

    级别字符串大小写不敏感；"WARN" 视作 "WARNING"。无效级别 warning 但不退出。
    "default" 是特殊键，作用在 root logger 上。
    """
    levels = cfg.get("log_levels")
    if not isinstance(levels, dict):
        return

    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    applied = []
    for name, level in levels.items():
        if not isinstance(name, str) or not isinstance(level, str):
            continue
        lvl = level.strip().upper()
        if lvl == "WARN":
            lvl = "WARNING"
        if lvl not in valid:
            _RELAY_LOGGER.warning("log_levels[%r] = %r is not a valid level, ignored", name, level)
            continue
        target = logging.getLogger() if name == "default" else logging.getLogger(name)
        target.setLevel(getattr(logging, lvl))
        applied.append(f"{name}={lvl}")

    if applied:
        _RELAY_LOGGER.info("Applied log_levels: %s", ", ".join(applied))


async def safe_close(writer: asyncio.StreamWriter | None) -> None:
    """
    异步静默关闭：close() + wait_closed()，带超时与异常吞咽。

    用 wait_closed 是为了在高并发短连接下让 OS 立即释放 fd，
    避免 asyncio 内部 ResourceWarning 与 fd 累积。
    """
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        return
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=_CLOSE_TIMEOUT)
    except Exception:
        pass


async def relay(reader_a: asyncio.StreamReader, writer_b: asyncio.StreamWriter,
                reader_b: asyncio.StreamReader, writer_a: asyncio.StreamWriter,
                label: str = "") -> None:
    """
    双向透明中继（无加密，用于直连路径）。

    异常处理：对端 RST / FIN / Timeout 在网络代理里是常规事件，**默认静默**，
    避免在正常断开时刷屏。但调用方可以打开 logger("utils") 的 DEBUG 级别
    把每条 leg 终止时的异常类型 + 消息打出来 —— 排查"代理莫名其妙就断了"
    类问题时再开，平时不必。label 用来在日志里区分是哪条连接的哪个方向。
    """
    async def pipe(reader, writer, direction):
        try:
            while True:
                data = await reader.read(_RELAY_BUF)
                if not data:
                    break
                writer.write(data)
                if writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
                    await writer.drain()
        except Exception as e:
            _RELAY_LOGGER.debug("relay %s %s ended: %s: %s",
                                label or "?", direction, type(e).__name__, e)
        finally:
            await safe_close(writer)

    task_a = asyncio.create_task(pipe(reader_a, writer_b, "local→remote"))
    task_b = asyncio.create_task(pipe(reader_b, writer_a, "remote→local"))
    try:
        await asyncio.wait([task_a, task_b], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (task_a, task_b):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
