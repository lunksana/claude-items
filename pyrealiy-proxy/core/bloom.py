"""
Bloom Filter —— 用于域名集合的快速成员判定预过滤

原理：
  哈希到 m 位的 bit 数组，k 个独立哈希函数。
  - 判断"不在集合"：100% 准确（无漏报）
  - 判断"在集合"：有极小概率误报（false positive），由 error_rate 控制

  因此只用作预过滤：BF 说"不在"→ 直接跳过（节省 dict 查询）；
  BF 说"可能在"→ 再去真实 dict 确认。

实现：
  使用 Kirsch-Mitzenmacher 双哈希技巧，从 blake2b 一次计算中派生 k 个哈希值，
  避免重复调用哈希函数。blake2b 在 Python 标准库中已有 C 实现，速度远快于 SHA256。
"""

from __future__ import annotations

import hashlib
import math


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
        digest = hashlib.blake2b(key.encode(), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8],  "little")
        h2 = int.from_bytes(digest[8:],  "little") | 1  # 保证 h2 为奇数，避免退化
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
