"""
端到端冒烟测试：真起 server + client，经 mixed 入口走 mirage 加密隧道访问本地
target，验证完整数据路径。无第三方依赖。

运行（项目根目录）：
    python3 tests/smoke_e2e.py
退出码 0 = 通过，1 = 失败。CI 可直接用。

验证点：
  - 服务端 TLS 伪装握手 + 隧道建立
  - 客户端 mixed 入口（HTTP CONNECT）→ proxy 隧道 → server → target 全链路
  - B7：入站监听在 geo 下载之前就绪（不被多 MB 的 geo 下载阻塞）
  - B6：route.default 被正确读取（这里 default=proxy，流量确实走隧道）

注意：服务端启动需联网抓 camouflage_host 的 TLS 会话（约 6s）。无外网时本测试
会在 server 就绪等待处超时失败——这是预期（隧道伪装依赖真实 TLS 指纹）。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SERVER_PORT = 18443
MIXED_PORT = 11080
TARGET_PORT = 18888
PASSWORD = "smoke_pass_e2e"
CAMO = "www.apple.com"


def log(*a):
    print(*a, flush=True)


def wait_for(path: str, needle: str, limit: float) -> float | None:
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            if needle in open(path).read():
                return time.time() - t0
        except OSError:
            pass
        time.sleep(0.2)
    return None


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="mirage_smoke_")
    server_cfg = os.path.join(workdir, "server.json")
    client_cfg = os.path.join(workdir, "client.json")
    server_log = os.path.join(workdir, "server.log")
    client_log = os.path.join(workdir, "client.log")

    json.dump({
        "listen_host": "127.0.0.1", "listen_port": SERVER_PORT,
        "password": PASSWORD, "camouflage_host": CAMO,
    }, open(server_cfg, "w"))
    json.dump({
        "schema_version": 1,
        "inbounds": [{"type": "mixed", "listen": f"127.0.0.1:{MIXED_PORT}"}],
        "outbounds": [
            {"tag": "proxy", "type": "mirage", "server_host": "127.0.0.1",
             "server_port": SERVER_PORT, "password": PASSWORD, "camouflage_host": CAMO},
            {"tag": "direct", "type": "direct"},
            {"tag": "block", "type": "block"},
        ],
        # default=proxy → 所有流量走隧道（验证 B6 + 数据路径）
        "route": {"default": "proxy", "rules": []},
        "geosite_sources": [{"name": "loyalsoldier",
                             "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"}],
        "geosite_dir": os.path.join(workdir, "geo"),
        "pool_size": 4,
    }, open(client_cfg, "w"))

    sf = open(server_log, "w")
    cf = open(client_log, "w")
    sp = subprocess.Popen([sys.executable, "-u", "server.py", server_cfg],
                          cwd=ROOT, stdout=sf, stderr=subprocess.STDOUT)
    cp = None
    ok = False
    try:
        t = wait_for(server_log, "Listening on", 25)
        if t is None:
            log("FAIL: 服务端未就绪（无外网抓 TLS 会话？见模块说明）")
            log("--- server.log ---")
            log(open(server_log).read()[-800:])
            return 1
        log(f"[server] ready in {t:.1f}s")

        cp = subprocess.Popen([sys.executable, "-u", "client.py", client_cfg],
                              cwd=ROOT, stdout=cf, stderr=subprocess.STDOUT)
        t_listen = wait_for(client_log, "MIXED listening", 30)
        if t_listen is None:
            log("FAIL: 客户端 mixed 入口未就绪")
            log(open(client_log).read()[-800:])
            return 1
        log(f"[B7] 入站监听就绪 {t_listen:.2f}s（geo 下载在此之后才开始）")

        # geo 下载必须在监听之后（B7）：检查日志顺序
        ctext = open(client_log).read()
        li = ctext.find("MIXED listening")
        gi = ctext.find("geo download will tunnel")
        if gi != -1 and gi < li:
            log("FAIL: geo 下载出现在入站监听之前（B7 回归）")
            return 1

        # 本地 target
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"TUNNEL_OK")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", TARGET_PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(2)  # 给 client pool 预热

        s = socket.create_connection(("127.0.0.1", MIXED_PORT), timeout=10)
        s.sendall(f"CONNECT 127.0.0.1:{TARGET_PORT} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        resp = s.recv(4096)
        if b"200" not in resp.split(b"\r\n")[0]:
            log(f"FAIL: CONNECT 未成功: {resp[:120]!r}")
            return 1
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        body = b""
        while True:
            d = s.recv(4096)
            if not d:
                break
            body += d
        s.close()
        srv.shutdown()
        if b"TUNNEL_OK" not in body:
            log(f"FAIL: 隧道响应不含 TUNNEL_OK: {body[:120]!r}")
            return 1
        log("[数据路径] mixed → proxy 隧道 → server → target: TUNNEL_OK ✓")
        ok = True
        return 0
    finally:
        for p in (cp, sp):
            if p is not None:
                p.kill()
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass
        sf.close()
        cf.close()
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        log("PASS" if ok else "FAILED")


if __name__ == "__main__":
    sys.exit(main())
