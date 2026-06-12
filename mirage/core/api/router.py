"""
最简路由：method × path 模式 → handler。

模式语法：
  /static/path     精确匹配
  /proxies/{name}  捕获到 `name` 参数（一个 path 段，不含 `/`）
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from .http_proto import Request, Response

# handler 签名：async (req, ctx, **path_params) → Response
Handler = Callable[..., Awaitable[Response]]

_PARAM_RE = re.compile(r"\{(\w+)\}")


class Router:
    def __init__(self) -> None:
        # HTTP 路由：(method, compiled_pattern, handler)
        self._routes: list[tuple[str, re.Pattern, Handler]] = []
        # WS 路由：(compiled_pattern, handler)。WS 永远是 GET，单独存
        self._ws_routes: list[tuple[re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        re_str = _PARAM_RE.sub(r"(?P<\1>[^/]+)", pattern)
        self._routes.append((method.upper(), re.compile(f"^{re_str}$"), handler))

    def add_ws(self, pattern: str, handler: Handler) -> None:
        """WS handler 签名：async (req, ctx, reader, writer, **path_params) → None"""
        re_str = _PARAM_RE.sub(r"(?P<\1>[^/]+)", pattern)
        self._ws_routes.append((re.compile(f"^{re_str}$"), handler))

    def match(self, method: str, path: str) -> tuple[Handler | None, dict[str, str], bool]:
        """返回 (handler, path_params, path_known)。path_known 用于 405 vs 404。"""
        method = method.upper()
        path_known = False
        for m, p, h in self._routes:
            mo = p.match(path)
            if mo:
                path_known = True
                if m == method:
                    return h, mo.groupdict(), True
        return None, {}, path_known

    def match_ws(self, path: str) -> tuple[Handler | None, dict[str, str]]:
        for p, h in self._ws_routes:
            mo = p.match(path)
            if mo:
                return h, mo.groupdict()
        return None, {}
