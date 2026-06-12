"""
mirage 吞吐与延迟基准

通过 SOCKS5 客户端 → 服务端 → 本地 target，测量：
  - 上行吞吐 (upload)：客户端→target 单向灌数据
  - 下行吞吐 (download)：target→客户端 单向灌数据
  - 双向吞吐 (bidir)：echo 模式同时收发
  - 延迟 (latency)：小包 ping-pong 测 P50/P95/P99

target 进程绑本地端口，由本脚本自启动。

用法：
  python bench.py                              # 全部场景，默认 16 并发，10s
  python bench.py --connections 1 --duration 5 # 单连接快测
  python bench.py --scenarios upload,latency   # 只跑指定场景
  python bench.py --json out.json              # 同时落盘 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import struct
import sys
import time

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 1080
TARGET_HOST = "127.0.0.1"
SINK_PORT = 19001    # 上行：丢弃收到的数据
SOURCE_PORT = 19002  # 下行：持续发送数据
ECHO_PORT = 19003    # 双向 / 延迟：原样回传

CHUNK_SIZE = 65536
LATENCY_PROBE_SIZE = 64
LATENCY_PROBES_PER_CONN = 200


# ============================================================
# 本地 target servers
# ============================================================

async def _sink_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK_SIZE)
            if not data:
                return
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _source_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    chunk = os.urandom(CHUNK_SIZE)
    try:
        while True:
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK_SIZE)
            if not data:
                return
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_targets() -> list[asyncio.AbstractServer]:
    servers = await asyncio.gather(
        asyncio.start_server(_sink_handler, TARGET_HOST, SINK_PORT),
        asyncio.start_server(_source_handler, TARGET_HOST, SOURCE_PORT),
        asyncio.start_server(_echo_handler, TARGET_HOST, ECHO_PORT),
    )
    return list(servers)


# ============================================================
# SOCKS5 客户端
# ============================================================

async def _socks5_connect(target_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection(SOCKS_HOST, SOCKS_PORT)
    w.write(b"\x05\x01\x00")
    await w.drain()
    auth = await r.readexactly(2)
    if auth != b"\x05\x00":
        raise RuntimeError(f"SOCKS5 auth rejected: {auth!r}")
    req = b"\x05\x01\x00\x01" + socket.inet_aton(TARGET_HOST) + struct.pack("!H", target_port)
    w.write(req)
    await w.drain()
    resp = await r.readexactly(10)
    if resp[1] != 0x00:
        raise RuntimeError(f"SOCKS5 connect rejected, rep={resp[1]:#x}")
    return r, w


async def _close(w: asyncio.StreamWriter) -> None:
    try:
        w.close()
        await w.wait_closed()
    except Exception:
        pass


# ============================================================
# 场景
# ============================================================

async def _upload_one(duration: float, stop_evt: asyncio.Event) -> int:
    r, w = await _socks5_connect(SINK_PORT)
    chunk = os.urandom(CHUNK_SIZE)
    sent = 0
    deadline = time.monotonic() + duration
    try:
        while not stop_evt.is_set() and time.monotonic() < deadline:
            w.write(chunk)
            await w.drain()
            sent += len(chunk)
    except Exception:
        pass
    finally:
        await _close(w)
    return sent


async def _download_one(duration: float, stop_evt: asyncio.Event) -> int:
    r, w = await _socks5_connect(SOURCE_PORT)
    received = 0
    deadline = time.monotonic() + duration
    try:
        while not stop_evt.is_set() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(r.read(CHUNK_SIZE), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            received += len(data)
    except Exception:
        pass
    finally:
        await _close(w)
    return received


async def _bidir_one(duration: float, stop_evt: asyncio.Event) -> tuple[int, int]:
    r, w = await _socks5_connect(ECHO_PORT)
    chunk = os.urandom(CHUNK_SIZE)
    sent = received = 0
    deadline = time.monotonic() + duration

    async def writer_task():
        nonlocal sent
        try:
            while not stop_evt.is_set() and time.monotonic() < deadline:
                w.write(chunk)
                await w.drain()
                sent += len(chunk)
        except Exception:
            pass

    async def reader_task():
        nonlocal received
        try:
            while not stop_evt.is_set() and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(r.read(CHUNK_SIZE), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                received += len(data)
        except Exception:
            pass

    try:
        await asyncio.gather(writer_task(), reader_task())
    finally:
        await _close(w)
    return sent, received


async def _latency_one(probes: int) -> list[float]:
    r, w = await _socks5_connect(ECHO_PORT)
    samples: list[float] = []
    try:
        # 先 warm up：等隧道完全建立，避免首包带握手延迟
        warmup = os.urandom(LATENCY_PROBE_SIZE)
        w.write(warmup)
        await w.drain()
        await r.readexactly(LATENCY_PROBE_SIZE)

        for _ in range(probes):
            payload = os.urandom(LATENCY_PROBE_SIZE)
            t0 = time.perf_counter()
            w.write(payload)
            await w.drain()
            echoed = await r.readexactly(LATENCY_PROBE_SIZE)
            dt = (time.perf_counter() - t0) * 1000.0  # ms
            if echoed == payload:
                samples.append(dt)
    except Exception:
        pass
    finally:
        await _close(w)
    return samples


# ============================================================
# 运行器
# ============================================================

def _mbps(total_bytes: int, seconds: float) -> float:
    return (total_bytes * 8) / (1024 * 1024 * seconds) if seconds > 0 else 0.0


async def run_throughput(name: str, runner, conns: int, duration: float) -> dict:
    stop = asyncio.Event()
    t0 = time.monotonic()
    tasks = [asyncio.create_task(runner(duration, stop)) for _ in range(conns)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t0

    sent_total = recv_total = 0
    errors = 0
    for r in results:
        if isinstance(r, Exception):
            errors += 1
            continue
        if isinstance(r, tuple):
            sent_total += r[0]
            recv_total += r[1]
        else:
            sent_total += r

    out = {
        "scenario": name,
        "connections": conns,
        "duration_s": round(elapsed, 3),
        "errors": errors,
    }
    if name == "upload":
        out["up_mbps"] = round(_mbps(sent_total, elapsed), 2)
        out["up_mb"] = round(sent_total / (1024 * 1024), 2)
    elif name == "download":
        out["down_mbps"] = round(_mbps(sent_total, elapsed), 2)  # sent_total here actually holds received
        out["down_mb"] = round(sent_total / (1024 * 1024), 2)
    elif name == "bidir":
        out["up_mbps"] = round(_mbps(sent_total, elapsed), 2)
        out["down_mbps"] = round(_mbps(recv_total, elapsed), 2)
        out["agg_mbps"] = round(_mbps(sent_total + recv_total, elapsed), 2)
    return out


async def run_latency(conns: int, probes_per_conn: int) -> dict:
    tasks = [asyncio.create_task(_latency_one(probes_per_conn)) for _ in range(conns)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_samples: list[float] = []
    errors = 0
    for r in results:
        if isinstance(r, Exception):
            errors += 1
            continue
        all_samples.extend(r)
    all_samples.sort()
    if not all_samples:
        return {"scenario": "latency", "errors": errors, "samples": 0}

    def pct(p):
        idx = int(len(all_samples) * p / 100)
        return all_samples[min(idx, len(all_samples) - 1)]

    return {
        "scenario": "latency",
        "connections": conns,
        "probes_per_conn": probes_per_conn,
        "samples": len(all_samples),
        "errors": errors,
        "p50_ms": round(pct(50), 3),
        "p95_ms": round(pct(95), 3),
        "p99_ms": round(pct(99), 3),
        "min_ms": round(all_samples[0], 3),
        "max_ms": round(all_samples[-1], 3),
        "mean_ms": round(statistics.mean(all_samples), 3),
    }


# ============================================================
# main
# ============================================================

async def amain(args) -> dict:
    print(f"[bench] starting target servers on ports {SINK_PORT}/{SOURCE_PORT}/{ECHO_PORT}")
    servers = await _start_targets()

    try:
        # Sanity ping: 确认 SOCKS5 client 可达
        try:
            r, w = await asyncio.wait_for(_socks5_connect(ECHO_PORT), timeout=3.0)
            await _close(w)
            print(f"[bench] SOCKS5 client at {SOCKS_HOST}:{SOCKS_PORT} reachable ✓")
        except Exception as e:
            print(f"[bench] FATAL: cannot reach SOCKS5 client: {e}", file=sys.stderr)
            return {"error": str(e)}

        if args.warmup_wait > 0:
            print(f"[bench] warmup wait {args.warmup_wait}s for pool refill ...")
            await asyncio.sleep(args.warmup_wait)

        scenarios = set(args.scenarios.split(",")) if args.scenarios else {"upload", "download", "bidir", "latency"}
        report: dict = {
            "uvloop": _running_uvloop(),
            "python": sys.version.split()[0],
            "connections": args.connections,
            "duration_s": args.duration,
            "results": [],
        }

        if "upload" in scenarios:
            print(f"\n[bench] upload — {args.connections} conn, {args.duration}s")
            res = await run_throughput("upload", _upload_one, args.connections, args.duration)
            print(f"  → {res['up_mbps']} Mbps ({res['up_mb']} MB) errors={res['errors']}")
            report["results"].append(res)

        if "download" in scenarios:
            print(f"\n[bench] download — {args.connections} conn, {args.duration}s")
            res = await run_throughput("download", _download_one, args.connections, args.duration)
            print(f"  → {res['down_mbps']} Mbps ({res['down_mb']} MB) errors={res['errors']}")
            report["results"].append(res)

        if "bidir" in scenarios:
            print(f"\n[bench] bidir — {args.connections} conn, {args.duration}s")
            res = await run_throughput("bidir", _bidir_one, args.connections, args.duration)
            print(f"  → up {res['up_mbps']} Mbps + down {res['down_mbps']} Mbps = agg {res['agg_mbps']} Mbps")
            report["results"].append(res)

        if "latency" in scenarios:
            print(f"\n[bench] latency — {min(args.connections, 8)} conn × {LATENCY_PROBES_PER_CONN} probes")
            res = await run_latency(min(args.connections, 8), LATENCY_PROBES_PER_CONN)
            if res.get("samples"):
                print(f"  → P50 {res['p50_ms']}ms  P95 {res['p95_ms']}ms  P99 {res['p99_ms']}ms  (n={res['samples']})")
            else:
                print(f"  → no samples (errors={res.get('errors')})")
            report["results"].append(res)

        return report

    finally:
        for s in servers:
            s.close()
        await asyncio.gather(*(s.wait_closed() for s in servers), return_exceptions=True)


def _running_uvloop() -> bool:
    try:
        import uvloop
        return isinstance(asyncio.get_event_loop_policy(), uvloop.EventLoopPolicy)
    except ImportError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="mirage throughput & latency benchmark")
    ap.add_argument("--connections", "-c", type=int, default=16)
    ap.add_argument("--duration", "-d", type=float, default=10.0)
    ap.add_argument("--scenarios", default="", help="comma list of: upload,download,bidir,latency (default all)")
    ap.add_argument("--json", default="", help="optional path to dump JSON report")
    ap.add_argument("--no-uvloop", action="store_true", help="force asyncio default loop")
    ap.add_argument("--warmup-wait", type=float, default=0.0,
                    help="seconds to wait after sanity ping before benching (lets pool refill between runs)")
    args = ap.parse_args()

    if not args.no_uvloop:
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass

    report = asyncio.run(amain(args))
    print("\n[bench] summary:")
    print(f"  uvloop  = {report.get('uvloop')}")
    print(f"  python  = {report.get('python')}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  wrote   = {args.json}")


if __name__ == "__main__":
    main()
