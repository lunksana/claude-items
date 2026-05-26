"""
Web 管理面板本地调试脚本

用法：
    python test_admin.py [--port 8080] [--token ""]

启动后在浏览器打开 http://127.0.0.1:<port>/
Ctrl+C 停止。

功能：
  - 用随机假数据填充所有面板，覆盖三种时长着色档位和多个流量级别
  - 每 3 秒随机更新一次活跃连接的流量计数，模拟真实波动
  - 支持面板上的全部操作（断开、封锁、解封）
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time

from core.admin import start_admin
from core.stats import ConnInfo, StatsStore

# ── 假数据配置 ────────────────────────────────────────────────────────────────

_ACTIVE_TARGETS = [
    ("github.com",        443, "203.0.113.10"),
    ("google.com",        443, "203.0.113.11"),
    ("youtube.com",       443, "203.0.113.12"),
    ("openai.com",        443, "203.0.113.13"),
    ("cloudflare.com",    443, "198.51.100.5"),
]

_RECENT_TARGETS = [
    ("twitter.com",   443, "10.0.0.2"),
    ("netflix.com",   443, "10.0.0.3"),
    ("wikipedia.org", 443, "10.0.0.4"),
    ("reddit.com",    443, "10.0.0.5"),
    ("discord.com",   443, "10.0.0.6"),
    ("telegram.org",  443, "10.0.0.7"),
]

_BLOCKED_IPS = ["1.2.3.4", "5.6.7.8"]

# 活跃连接时长（秒）：覆盖三种着色档位
#   < 30s   → 默认色
#   30-300s → 琥珀
#   > 300s  → 绿色
_DURATIONS = [8, 45, 400, 12, 280]


def _populate(store: StatsStore) -> None:
    # ── 活跃连接 ──────────────────────────────────────────────────────────────
    for i, (host, port, ip) in enumerate(_ACTIVE_TARGETS):
        conn = store.register(ip, host, port)
        conn.bytes_up   = random.randint(512 * 1024, 30 * 1024 * 1024)
        conn.bytes_down = random.randint(1024 * 1024, 150 * 1024 * 1024)
        # 按预设时长倒推 started，使每条连接落入不同着色区间
        conn.started = time.monotonic() - _DURATIONS[i % len(_DURATIONS)]

    # ── 连接记录（最近关闭）────────────────────────────────────────────────────
    for host, port, ip in _RECENT_TARGETS:
        store._counter += 1
        store.total_conns += 1
        c = ConnInfo(store._counter, ip, host, port)
        c.bytes_up   = random.randint(1024, 5 * 1024 * 1024)
        c.bytes_down = random.randint(1024, 20 * 1024 * 1024)
        store.recent.appendleft(
            c.as_dict(closed_at=time.time() - random.randint(10, 600))
        )
        store.domain_conns[host] += random.randint(1, 30)
        store.domain_bytes[host] += c.bytes_up + c.bytes_down

    # 活跃连接的域名也计入分布
    for host, _, _ in _ACTIVE_TARGETS:
        store.domain_conns[host] += random.randint(1, 10)
        store.domain_bytes[host] += random.randint(0, 50 * 1024 * 1024)

    # ── 封锁 IP ───────────────────────────────────────────────────────────────
    for ip in _BLOCKED_IPS:
        store.blocked.add(ip)


async def _traffic_ticker(store: StatsStore, interval: float = 3.0) -> None:
    """每隔 interval 秒随机增加活跃连接的流量，模拟实时波动。"""
    while True:
        await asyncio.sleep(interval)
        for conn in store.active.values():
            conn.bytes_up   += random.randint(0, 512 * 1024)
            conn.bytes_down += random.randint(0, 2 * 1024 * 1024)


async def main(port: int, token: str) -> None:
    store = StatsStore()
    _populate(store)

    server = await start_admin(store, "0.0.0.0", port, token)
    token_hint = f"?token={token}" if token else ""
    print(f"Admin panel: http://0.0.0.0:{port}/{token_hint}")
    print("Ctrl+C to stop\n")
    print("Panels populated with:")
    print(f"  {len(store.active)} active connections")
    print(f"  {len(store.recent)} recent connections")
    print(f"  {len(store.domain_conns)} domains in distribution")
    print(f"  {len(store.blocked)} blocked IPs")

    async with server:
        await asyncio.gather(
            server.serve_forever(),
            _traffic_ticker(store),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Admin panel local test server")
    parser.add_argument("--port",  type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--token", type=str, default="",   help="Access token (default: none)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.port, args.token))
    except KeyboardInterrupt:
        print("\nStopped.")
