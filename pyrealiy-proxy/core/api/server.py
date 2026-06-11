"""
APIServer：asyncio HTTP/1.1 服务，挂在 cfg["api"]["listen"]。

职责：
  - listen + accept，每条连接处理 keep-alive 串行请求
  - Bearer Token 鉴权（`Authorization: Bearer <secret>` 或 `?token=<secret>`）
  - CORS（preflight OPTIONS 自动响应；正常响应加 Access-Control-Allow-Origin）
  - 调路由
  - 异常 → 对应 status

不在这一层做的：业务逻辑。所有 /version /configs /proxies /... 在 endpoints 模块里。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from .http_proto import HTTPError, Request, Response, read_request, write_response
from .router import Router

logger = logging.getLogger("api")

# keep-alive 闲置上限：客户端 hold 住连接不发就关
_IDLE_KEEPALIVE_SEC = 75


@dataclass
class APIContext:
    """传给所有 endpoint 的运行时引用集合。后续 Phase 会扩。"""
    version: str
    cfg: dict             # 完整 cfg（含 schema_v1 各 section）
    outbounds: Any = None # build_outbounds 结果，P3 用
    router_engine: Any = None  # 业务 router（route 模块），P3 用
    registry: Any = None       # ConnectionRegistry，P2 用
    log_broadcaster: Any = None  # LogBroadcaster，P4 WS /logs 用
    routing_cache: Any = None  # RoutingCache，L3 路由决策缓存
    dns_cache: Any = None      # DnsCache，L3 DNS 响应缓存
    reloader: Any = None       # Reloader，L4 配置热加载
    pool_view: Any = None      # P5 用
    handshake_view: Any = None # P5 用
    timesync: Any = None       # P5 用


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status=status, body=body, content_type="application/json")


def text_response(text: str, status: int = 200) -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type="text/plain; charset=utf-8")


class APIServer:
    def __init__(self, cfg: dict, ctx: APIContext):
        api_cfg = cfg.get("api") or {}
        self._listen = api_cfg["listen"]                      # load_config 已校验
        self._secret = api_cfg["secret"]                      # load_config 已校验
        self._cors = api_cfg.get("cors", ["*"]) or ["*"]
        self._ctx = ctx
        self._router = Router()
        self._server: asyncio.base_events.Server | None = None
        self._register_endpoints()

    def _register_endpoints(self) -> None:
        # 延迟 import 防循环
        from . import clash_endpoints, ws_endpoints, pyrealiy_endpoints
        clash_endpoints.register(self._router, self._ctx)
        pyrealiy_endpoints.register(self._router, self._ctx)
        ws_endpoints.register(self._router, self._ctx)

    async def start(self) -> None:
        host, port = _parse_listen(self._listen)
        self._server = await asyncio.start_server(self._handle_conn, host, port)
        logger.info("API listening on %s:%d (Bearer auth, CORS: %s)",
                    host, port, ", ".join(self._cors))

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass

    # ---------- 连接处理 ----------

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    req = await asyncio.wait_for(read_request(reader), timeout=_IDLE_KEEPALIVE_SEC)
                except asyncio.TimeoutError:
                    return  # keep-alive 闲置超时
                if req is None:
                    return  # EOF

                # WS 升级：拿走 reader/writer，handler 接管，本协程随 handler 退出
                if req.method == "GET" and "websocket" in req.header("upgrade").lower():
                    handled = await self._maybe_dispatch_ws(req, reader, writer)
                    if handled:
                        return

                resp = await self._dispatch(req)
                keep_alive = self._should_keep_alive(req, resp)
                self._inject_cors(req, resp)
                await write_response(writer, resp, keep_alive=keep_alive)
                if not keep_alive:
                    return
        except HTTPError as e:
            try:
                resp = json_response({"code": e.status, "message": e.message}, status=e.status)
                resp.close_after = True
                await write_response(writer, resp, keep_alive=False)
            except Exception:
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("API: unhandled exception from %s", peer)
            try:
                resp = json_response({"code": 500, "message": "internal error"}, status=500)
                resp.close_after = True
                await write_response(writer, resp, keep_alive=False)
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _maybe_dispatch_ws(self, req: Request, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> bool:
        """
        匹配上 WS 路由就完成握手并把控制权交给 handler。
        返回 True 表示已处理（caller 不再走 HTTP loop）。
        """
        handler, params = self._router.match_ws(req.path)
        if handler is None:
            return False

        if not self._check_auth(req):
            resp = json_response({"code": 401, "message": "unauthorized"}, status=401)
            resp.close_after = True
            self._inject_cors(req, resp)
            await write_response(writer, resp, keep_alive=False)
            return True

        from .ws_proto import perform_handshake
        try:
            await perform_handshake(req, writer)
        except HTTPError as e:
            resp = json_response({"code": e.status, "message": e.message}, status=e.status)
            resp.close_after = True
            await write_response(writer, resp, keep_alive=False)
            return True

        try:
            await handler(req, self._ctx, reader, writer, **params)
        except Exception:
            logger.exception("API: WS handler crashed")
        return True

    async def _dispatch(self, req: Request) -> Response:
        # CORS preflight
        if req.method == "OPTIONS":
            return Response(status=204)

        # Auth（所有路径，包括 /version）
        if not self._check_auth(req):
            return json_response({"code": 401, "message": "unauthorized"}, status=401)

        handler, params, path_known = self._router.match(req.method, req.path)
        if handler is None:
            if path_known:
                return json_response({"code": 405, "message": "method not allowed"}, status=405)
            return json_response({"code": 404, "message": "not found"}, status=404)

        return await handler(req, self._ctx, **params)

    # ---------- 鉴权 / CORS ----------

    def _check_auth(self, req: Request) -> bool:
        # Authorization: Bearer <secret>
        auth = req.header("authorization")
        if auth.startswith("Bearer "):
            return _const_eq(auth[7:].strip(), self._secret)
        # ?token=<secret> 兼容 Yacd 历史用法
        tok = req.query.get("token")
        if tok:
            return _const_eq(tok, self._secret)
        return False

    def _inject_cors(self, req: Request, resp: Response) -> None:
        origin = req.header("origin")
        if not origin:
            return
        allow = self._cors_allow_for(origin)
        if not allow:
            return
        resp.extra_headers.append(("Access-Control-Allow-Origin", allow))
        resp.extra_headers.append(("Access-Control-Allow-Credentials", "true"))
        if req.method == "OPTIONS":
            resp.extra_headers.append(("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS"))
            resp.extra_headers.append(("Access-Control-Allow-Headers", "Content-Type, Authorization"))
            resp.extra_headers.append(("Access-Control-Max-Age", "86400"))

    def _cors_allow_for(self, origin: str) -> str | None:
        if "*" in self._cors:
            return "*"
        return origin if origin in self._cors else None

    @staticmethod
    def _should_keep_alive(req: Request, resp: Response) -> bool:
        if resp.close_after:
            return False
        return req.header("connection", "keep-alive").lower() != "close"


# ---------- 辅助 ----------

def _parse_listen(s: str) -> tuple[str, int]:
    """支持 `host:port` 和 `[ipv6]:port`。"""
    if s.startswith("["):
        host_end = s.index("]")
        host = s[1:host_end]
        port = int(s[host_end + 2:])
        return host, port
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s)


def _const_eq(a: str, b: str) -> bool:
    """常量时间比较，防 timing 攻击（管理 secret）。"""
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode(), b.encode()):
        r |= x ^ y
    return r == 0
