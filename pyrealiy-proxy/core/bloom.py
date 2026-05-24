"""
Bloom Filter —— 用于域名集合的快速成员判定预过滤

原理：
  哈希到 m 位的 bit 数组，k 个独立哈希函数。
  - 判断"不在集合"：100% 准确（无漏报）
  - 判断"在集合"：有极小概率误报（false positive），由 error_rate 控制

  因此只用作预过滤：BF 说"不在"→ 直接跳过（节省 dict 查询）；
  BF 说"可能在"→ 再去真实 dict 确认。

实现：
  使用 Kirsch-Mitzenmacher 双哈希技巧派生 k 个位置。
  哈希采用 Python 内置 hash()（C 层，字符串对象缓存结果）+ finalizer 混合出
  第二个独立值，比 blake2b 快且无外部依赖。
  hash() 受 PYTHONHASHSEED 影响，但 BF 在同一进程内构建并查询，完全一致。
"""

from __future__ import annotations

import math

# 64-bit 掩码
_M64 = 0xFFFFFFFFFFFFFFFF


def _hash_pair(key: str) -> tuple[int, int]:
    """从 Python 内置 hash 派生两个独立的 64-bit 值（Murmur finalizer 混合）"""
    h = hash(key) & _M64
    # Wang/Murmur finalizer：将 h 扩散为 h2，保证 h1 ≠ h2 且分布均匀
    h2 = h ^ (h >> 30)
    h2 = (h2 * 0xBF58476D1CE4E5B9) & _M64
    h2 = h2 ^ (h2 >> 27)
    h2 = (h2 * 0x94D049BB133111EB) & _M64
    h2 = (h2 ^ (h2 >> 31)) | 1  # 保证奇数，避免 Kirsch-Mitzenmacher 退化
    return h, h2


class BloomFilter:
    def __init__(self, capacity: int, error_rate: float = 0.005):
        """
        capacity:   预期最大元素数
        error_rate: 可接受的误报率（默认 0.5%）
        """
        assert 0 < error_rate < 1
        assert capacity > 0

        # 最优 bit 数 m 和哈希函数数 k
        m = math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        k = max(1, round((m / capacity) * math.log(2)))

        self._m    = m
        self._k    = k
        self._bits = bytearray((m + 7) >> 3)

    # ── 内部哈希 ──────────────────────────────────────────────────────────────

    def _positions(self, key: str):
        """Kirsch-Mitzenmacher: g_i(x) = h1(x) + i·h2(x) mod m"""
        h1, h2 = _hash_pair(key)
        for i in range(self._k):
            yield (h1 + i * h2) % self._m

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def add(self, key: str) -> None:
        for p in self._positions(key):
            self._bits[p >> 3] |= 1 << (p & 7)

    def __contains__(self, key: str) -> bool:
        return all(self._bits[p >> 3] & (1 << (p & 7)) for p in self._positions(key))

    @property
    def bit_count(self) -> int:
        return self._m
