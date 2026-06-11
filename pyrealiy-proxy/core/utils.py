"""工具函数：日志、字节操作等"""


from __future__ import annotations

import datetime as _dt
import json as _json
import logging

import socket

import struct

import time

import asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ── 结构化日志 ────────────────────────────────────────────────────────────────

# LogRecord 的内置字段，从 record.__dict__ 里减掉这些就剩下 extra=
_LOG_RECORD_BUILTIN_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JsonFormatter(logging.Formatter):
    """
    输出每行一个 JSON：
      {"ts": "ISO-8601 UTC","level":"info","logger":"conn_pool",
       "msg":"Pool warmed up: 20/20","extra":{...}}

    `extra` 字段来自 `logger.info("...", extra={"k":"v"})`。无 extra 时不出现该键。
    异常用 "exc" 字段附 traceback 文本。

    用法：见 apply_log_format(cfg) —— cfg["log"]["format"] == "json" 时切换。
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S."
        ) + f"{int(record.msecs):03d}Z"
        out: dict = {
            "ts":     ts,
            "level":  record.levelname.lower(),
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _LOG_RECORD_BUILTIN_ATTRS and not k.startswith("_")
        }
        if extras:
            out["extra"] = extras
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return _json.dumps(out, ensure_ascii=False, default=str)


def apply_log_format(cfg: dict) -> None:
    """
    根据 cfg["log"]["format"] 切换日志格式：
      "text" / 缺省  → basicConfig 的原格式（向后兼容）
      "json"         → JsonFormatter（每行一个 JSON）

    替换所有 root logger 已挂的 handler 的 formatter。需要在任何业务日志前调。
    """
    log_cfg = (cfg.get("log") or {})
    fmt = str(log_cfg.get("format", "text")).lower()
    if fmt not in ("text", "json"):
        logging.getLogger("utils").warning(
            "log.format must be 'text' or 'json', got %r; using text", fmt
        )
        return
    if fmt == "text":
        return  # basicConfig 已是 text
    json_fmt = JsonFormatter()
    for h in logging.getLogger().handlers:
        h.setFormatter(json_fmt)


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


_RELAY_BUF        = 32768      # 单次读取大小，平衡延迟与吞吐
_DRAIN_THRESHOLD  = 64 * 1024  # 写缓冲积压超过此值才 drain，避免每帧切换协程
_CLOSE_TIMEOUT    = 2.0        # wait_closed 上限：避免对端不发 FIN 时永久挂起
_DRAIN_AFTER_HALF = 2.0        # 单向结束后，给另一方向最多多少秒优雅退出（避免 FIN 后 RST 指纹）

# utils 模块自身用的 logger：
#   - 中继协程异常的可选 debug 输出（用户开启 DEBUG 后可见）
#   - apply_log_levels() 自身的反馈与告警
# 模块级单例，名字直接对应 cfg["log_levels"] 里的 "utils" 键
_logger = get_logger("utils")


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
    if levels is None:
        return
    if not isinstance(levels, dict):
        _logger.warning("log_levels must be an object (got %s), ignored", type(levels).__name__)
        return

    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    applied = []
    for name, level in levels.items():
        # 类型守门：JSON null / 数字 / 列表都警告 + 跳过，避免静默吞配置错
        if not isinstance(name, str):
            _logger.warning("log_levels key %r is not a string, ignored", name)
            continue
        if not isinstance(level, str):
            _logger.warning("log_levels[%r] = %r is not a string, ignored "
                            "(use \"DEBUG\" / \"INFO\" / ...)", name, level)
            continue

        name_clean = name.strip()
        lvl = level.strip().upper()
        if lvl == "WARN":
            lvl = "WARNING"
        if lvl not in valid:
            _logger.warning("log_levels[%r] = %r is not a valid level, ignored", name, level)
            continue
        if not name_clean:
            _logger.warning("log_levels has empty key (level=%r), ignored", level)
            continue

        target = logging.getLogger() if name_clean == "default" else logging.getLogger(name_clean)
        target.setLevel(getattr(logging, lvl))
        applied.append(f"{name_clean}={lvl}")

    if applied:
        _logger.info("Applied log_levels: %s", ", ".join(applied))


async def safe_close(writer: asyncio.StreamWriter | None,
                     reader: asyncio.StreamReader | None = None) -> None:
    """
    优雅静默关闭，**避免触发 RST**。

    ── 为什么 0.4.5 的修复还不够 ───────────────────────────────────────────
    Linux TCP 规约：**close() 时若 OS 接收缓冲区仍有未消费数据，会发 RST
    而非 FIN**。`write_eof()` 只半关写端、不解决接收侧。

    0.4.5 客户端 RST 224→9 是因为客户端方向的 cancel 路径下，对端协程被
    cancel 时往往已读完了所有 record。但**服务端**方向：target 在不停下行、
    `target_to_tunnel` 被 grace 超时 cancel 时，`client_reader` 的 OS 接收
    缓冲里**还有 client 没被消费的字节**（前个 record 没被 tunnel.recv 拿走、
    或 client 后续发的 close_notify / 数据帧），close → RST 给 client。

    抓包（0605.pcap）证实：52 个服务端 RST 距最后一条 payload 中位 1.98s，
    精确命中 _DRAIN_AFTER_HALF = 2.0 的超时点。

    ── 五步优雅关 ──────────────────────────────────────────────────────────
      1. write_eof   —— 半关写端发 FIN
      2. drain       —— 冲完我们自己的发送缓冲
      3. drain reader（可选，关键）—— **真正读光接收缓冲**，避免 close → RST
      4. close       —— 释放 socket
      5. wait_closed —— 等内核回收
    """
    if writer is None:
        return
    try:
        if writer.is_closing():
            return
    except Exception:
        pass
    # 1) 发 FIN
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except Exception:
        pass
    # 2) 冲完发送缓冲
    try:
        await asyncio.wait_for(writer.drain(), timeout=_CLOSE_TIMEOUT)
    except Exception:
        pass
    # 3) drain 接收缓冲（若提供 reader）—— 最多 0.5s，best-effort
    #    把对端在 grace 期间继续推过来的字节读光，让 close 不带未读数据
    if reader is not None:
        try:
            deadline = time.monotonic() + 0.5
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
                if not chunk:
                    break  # EOF 自然到达
        except Exception:
            pass
    # 4) 关 socket
    try:
        writer.close()
    except Exception:
        return
    # 5) 等内核回收
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=_CLOSE_TIMEOUT)
    except Exception:
        pass


async def wait_both_with_grace(task_a: asyncio.Task, task_b: asyncio.Task,
                               grace: float = _DRAIN_AFTER_HALF) -> None:
    """
    等任一中继协程结束后，给另一个最多 `grace` 秒优雅退出，否则 cancel。

    用于双向中继：先 await FIRST_COMPLETED，然后让对端方向有机会读完 server
    剩余数据 / 发送 close_notify、自然返回。这样最后 safe_close 时接收缓冲
    已空，不再触发 RST。
    """
    await asyncio.wait([task_a, task_b], return_when=asyncio.FIRST_COMPLETED)
    pending = {t for t in (task_a, task_b) if not t.done()}
    if not pending:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=grace,
        )
    except asyncio.TimeoutError:
        for t in pending:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass


async def relay(reader_a: asyncio.StreamReader, writer_b: asyncio.StreamWriter,
                reader_b: asyncio.StreamReader, writer_a: asyncio.StreamWriter,
                label: str = "", on_up=None, on_down=None) -> None:
    """
    双向透明中继（无加密，用于直连路径）。

    异常处理：对端 RST / FIN / Timeout 在网络代理里是常规事件，**默认静默**，
    避免在正常断开时刷屏。但调用方可以打开 logger("utils") 的 DEBUG 级别
    把每条 leg 终止时的异常类型 + 消息打出来 —— 排查"代理莫名其妙就断了"
    类问题时再开，平时不必。label 用来在日志里区分是哪条连接的哪个方向。

    关闭顺序（避免 RST 指纹）：
      1. 任一 pipe 协程结束时**只发 FIN**（write_eof），不直接 close
      2. 由外层 wait_both_with_grace 等另一 pipe 也自然退出（最多 2s）
      3. 然后两边一并 safe_close —— 此时接收缓冲已被对方 pipe 排空
    """
    async def pipe(reader, writer, direction, on_bytes):
        try:
            while True:
                data = await reader.read(_RELAY_BUF)
                if not data:
                    break
                writer.write(data)
                if on_bytes:
                    on_bytes(len(data))
                if writer.transport.get_write_buffer_size() > _DRAIN_THRESHOLD:
                    await writer.drain()
        except Exception as e:
            _logger.debug("relay %s %s ended: %s: %s",
                          label or "?", direction, type(e).__name__, e)
        # 发我方 FIN 让对端的 reader 尽快 EOF；不 close（外层统一）
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass

    task_a = asyncio.create_task(pipe(reader_a, writer_b, "local→remote", on_up))
    task_b = asyncio.create_task(pipe(reader_b, writer_a, "remote→local", on_down))
    try:
        await wait_both_with_grace(task_a, task_b)
    finally:
        # safe_close 带 reader：在 close 之前 drain 接收缓冲，避免 close → RST
        await safe_close(writer_a, reader_a)
        await safe_close(writer_b, reader_b)
