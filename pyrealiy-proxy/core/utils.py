"""工具函数：日志、字节操作等"""


from __future__ import annotations

import logging

import struct

import asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


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


async def relay(reader_a: asyncio.StreamReader, writer_b: asyncio.StreamWriter,
                reader_b: asyncio.StreamReader, writer_a: asyncio.StreamWriter) -> None:
    """双向透明中继（无加密，用于握手阶段转发）"""
    async def pipe(reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    task_a = asyncio.create_task(pipe(reader_a, writer_b))
    task_b = asyncio.create_task(pipe(reader_b, writer_a))
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
