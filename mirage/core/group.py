"""
Outbound 组节点（sing-box 风格 urltest / fallback）

UrlTestGroup（type=urltest）:
  目标：在 children 中选 median latency 最低的。
  防抖（tolerance）：current.latency − best.latency < tolerance_ms 时保持
        current 选择，避免相近延迟下频繁切换破坏 TCP 长连接复用。
  延迟数据：来自每个 child（mirage outbound）BrutalPool build 时间的
        滚动样本中位数；无样本的 child 不参与排序但仍可被首选。

FallbackGroup（type=fallback）:
  目标：按声明顺序选第一个 is_healthy=True 的 child。
  全部 unhealthy 时回退到首个（避免无路可走；端到端层会自然报错）。

嵌套：children 可以是另一个组。resolve_leaf() 递归展开到叶子。
"""

from __future__ import annotations

from typing import Optional

from .outbound import Outbound
from .utils import get_logger, safe_close

logger = get_logger("group")


class _GroupBase(Outbound):
    """组节点的共同基类"""

    def __init__(self, tag: str, children: list[Outbound]):
        self.tag = tag
        self._children = children

    async def warmup(self) -> None:
        # children 已在 build_outbounds 外层逐一 warmup，组本身无需启动
        return

    @property
    def is_healthy(self) -> bool:
        return any(c.is_healthy for c in self._children)

    @property
    def latency_ms(self) -> Optional[float]:
        leaf = self.resolve_leaf()
        return leaf.latency_ms if leaf is not self else None

    async def handle(self, local_reader, local_writer, target_host, target_port):
        leaf = self.resolve_leaf()
        if leaf is self:
            logger.error("[%s] no healthy child", self.tag)
            await safe_close(local_writer)
            return
        await leaf.handle(local_reader, local_writer, target_host, target_port)


class UrlTestGroup(_GroupBase):
    type = "urltest"

    def __init__(self, tag: str, children: list[Outbound], tolerance_ms: int = 50):
        super().__init__(tag, children)
        self._tolerance = tolerance_ms
        # current 初值取首个 child，避免没有延迟样本时随机表现
        self._current: Optional[Outbound] = children[0] if children else None

    def resolve_leaf(self) -> Outbound:
        candidates = [c for c in self._children if c.is_healthy]
        if not candidates:
            return self

        with_lat = [(c, c.latency_ms) for c in candidates if c.latency_ms is not None]

        # 都没延迟样本：保持 current（若它还在 healthy 集合中），否则取首个
        if not with_lat:
            if self._current in candidates:
                return self._current.resolve_leaf()
            self._current = candidates[0]
            return self._current.resolve_leaf()

        best, best_lat = min(with_lat, key=lambda x: x[1])

        # tolerance 防抖：current 还可用且延迟差 < tolerance → 保持
        if (self._current in candidates
                and self._current.latency_ms is not None
                and self._current.latency_ms - best_lat < self._tolerance):
            return self._current.resolve_leaf()

        if self._current is not best:
            logger.info("[%s] switch %s (%.0fms) -> %s (%.0fms)",
                        self.tag,
                        self._current.tag if self._current else "?",
                        self._current.latency_ms if (self._current and self._current.latency_ms is not None) else -1,
                        best.tag, best_lat)
        self._current = best
        return best.resolve_leaf()


class FallbackGroup(_GroupBase):
    type = "fallback"

    def resolve_leaf(self) -> Outbound:
        for c in self._children:
            if c.is_healthy:
                return c.resolve_leaf()
        # 全部 unhealthy：回退到首个（避免无路可走，让端到端层报真实错误）
        return self._children[0].resolve_leaf() if self._children else self
