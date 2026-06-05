"""PyReality 版本号。单一来源（single source of truth）。

约定：
  - SemVer 语义：MAJOR.MINOR.PATCH
  - 现仍处 0.x：协议 / 配置 schema 可能在 MINOR 升级时变动，向后兼容由
    每条 CHANGELOG 条目单独说明
  - 仅在 client.py / server.py 启动 banner 与 admin panel header 使用
"""

__version__ = "0.4.7"
