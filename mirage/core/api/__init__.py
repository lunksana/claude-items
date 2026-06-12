"""
Mirage Clash 兼容 API（client 端只读）。

入口：APIServer。如果 cfg["api"]["listen"] 缺失则不启动。
"""

from .server import APIServer  # noqa: F401
