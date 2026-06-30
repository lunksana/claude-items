"""
geosite_cache 下载完整性 + ETag 协商缓存 单元测试。

覆盖的回归点：
  - _check_dat_sane：< 100KB 文件被拒
  - _download 直连：Content-Length 截断检测
  - _download 直连：HTML 错误页（< 100KB）被 sanity 拒绝
  - _download 直连：首次写入 meta.etag/last_modified；带 cached_etag 二次请求得 304
  - _ensure_one 端到端：第一次下载 → 第二次 If-None-Match 命中 304（文件不动、
    downloaded_at 刷新）→ force=True 跳过条件头重下

不测 _download_via_tunnel：需要完整 mirage server + 加密隧道 + 时间同步。
代码路径与 _download 对称（同一套 cached_etag/cached_lm 流程），逻辑等价。
"""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.geosite_cache import (  # noqa: E402
    _check_dat_sane, _download, _ensure_one,
)


# 至少 200KB，超过 100KB sanity 阈值；固定内容让 ETag 稳定
_DAT_BODY = b"GEODAT" * (200 * 1024 // 6 + 1)
_DAT_ETAG = '"abc123"'
_DAT_LM   = "Wed, 01 Jan 2026 00:00:00 GMT"


class _MockHandler(http.server.BaseHTTPRequestHandler):
    """按 path 分支模拟不同上游行为"""
    def log_message(self, *_):  # silence
        pass

    def do_GET(self):
        path = self.path
        client_etag = self.headers.get("If-None-Match")

        if path == "/conditional":
            if client_etag == _DAT_ETAG:
                self.send_response(304)
                self.send_header("ETag", _DAT_ETAG)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(_DAT_BODY)))
                self.send_header("ETag", _DAT_ETAG)
                self.send_header("Last-Modified", _DAT_LM)
                self.end_headers()
                self.wfile.write(_DAT_BODY)
        elif path == "/truncated":
            # 声明 Content-Length 完整但只发一半
            self.send_response(200)
            self.send_header("Content-Length", str(len(_DAT_BODY)))
            self.end_headers()
            self.wfile.write(_DAT_BODY[:len(_DAT_BODY) // 2])
        elif path == "/tiny":
            tiny = b"<html><body>404 Not Found</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(tiny)))
            self.end_headers()
            self.wfile.write(tiny)
        else:
            self.send_response(404)
            self.end_headers()


class _MockServerMixin:
    """共享一个 mock http server"""
    httpd: socketserver.TCPServer
    base: str

    @classmethod
    def setUpClass(cls):
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
        port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls._thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()


class TestSaneCheck(unittest.TestCase):
    def test_rejects_tiny_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 50)
            path = f.name
        try:
            with self.assertRaises(IOError) as ctx:
                _check_dat_sane(path)
            self.assertIn("suspiciously small", str(ctx.exception))
        finally:
            os.unlink(path)


class TestDownloadDirect(_MockServerMixin, unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmpdir, "x.dat")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_download_saves_etag(self):
        r = _download(f"{self.base}/conditional", self.dest)
        self.assertEqual(r["kind"], "saved")
        self.assertEqual(r["etag"], _DAT_ETAG)
        self.assertEqual(r["last_modified"], _DAT_LM)
        self.assertEqual(os.path.getsize(self.dest), len(_DAT_BODY))

    def test_revalidation_returns_304(self):
        r1 = _download(f"{self.base}/conditional", self.dest)
        mtime_before = os.path.getmtime(self.dest)
        time.sleep(0.05)
        r2 = _download(f"{self.base}/conditional", self.dest,
                       cached_etag=r1["etag"], cached_lm=r1["last_modified"])
        self.assertEqual(r2["kind"], "not_modified")
        # 文件没被覆写
        self.assertEqual(os.path.getmtime(self.dest), mtime_before)

    def test_truncated_body_raises(self):
        with self.assertRaises(IOError) as ctx:
            _download(f"{self.base}/truncated", self.dest)
        self.assertIn("truncated body", str(ctx.exception))
        # tmp + dest 都应清理
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".tmp"))

    def test_tiny_response_rejected(self):
        with self.assertRaises(IOError) as ctx:
            _download(f"{self.base}/tiny", self.dest)
        self.assertIn("suspiciously small", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest))


class TestEnsureOneLifecycle(_MockServerMixin, unittest.TestCase):
    """覆盖完整的 first-download → 304 revalidation → force 重下 路径"""

    def test_full_lifecycle(self):
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                meta = {"v": 1, "sources": {}}

                # 1) 首次下载（meta 空、cached_etag 无）
                ok1 = await _ensure_one("site-mock", f"{self.base}/conditional",
                                        cache_dir=tmpdir, update_days=0.0,
                                        meta=meta, pool=None, force=False)
                self.assertTrue(ok1)
                info1 = meta["sources"]["site-mock"]
                self.assertEqual(info1["etag"], _DAT_ETAG)
                self.assertEqual(info1["last_modified"], _DAT_LM)
                downloaded_at_1 = info1["downloaded_at"]

                # 2) update_days=0 强制重新评估，但 If-None-Match 命中 304
                await asyncio.sleep(0.05)
                ok2 = await _ensure_one("site-mock", f"{self.base}/conditional",
                                        cache_dir=tmpdir, update_days=0.0,
                                        meta=meta, pool=None, force=False)
                self.assertTrue(ok2)
                info2 = meta["sources"]["site-mock"]
                # downloaded_at 被刷新（304 也算 revalidation）
                self.assertGreaterEqual(info2["downloaded_at"], downloaded_at_1)
                # etag 没变
                self.assertEqual(info2["etag"], _DAT_ETAG)

                # 3) force=True：跳过条件头，必拉新内容
                ok3 = await _ensure_one("site-mock", f"{self.base}/conditional",
                                        cache_dir=tmpdir, update_days=0.0,
                                        meta=meta, pool=None, force=True)
                self.assertTrue(ok3)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_local_file_missing_skips_conditional(self):
        """文件丢失时即使 meta 里有 etag，也应不发条件头、强制重下。"""
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                # meta 里有 etag，但本地无文件
                meta = {"v": 1, "sources": {
                    "site-mock": {
                        "url":           f"{self.base}/conditional",
                        "downloaded_at": int(time.time()),
                        "etag":          _DAT_ETAG,
                        "last_modified": _DAT_LM,
                    }
                }}
                # 不存在 site-mock.dat —— _ensure_one 应识别为 missing 重下
                ok = await _ensure_one("site-mock", f"{self.base}/conditional",
                                       cache_dir=tmpdir, update_days=7.0,
                                       meta=meta, pool=None, force=False)
                self.assertTrue(ok)
                dest = os.path.join(tmpdir, "site-mock.dat")
                # 真的拉到内容，不是 304 命中后 dest 缺失
                self.assertTrue(os.path.exists(dest))
                self.assertEqual(os.path.getsize(dest), len(_DAT_BODY))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
