"""交易日历。"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import date


class TradingCalendar:
    """由交易日序列构成的日历。"""

    def __init__(self, days: Sequence[date]) -> None:
        self._days: list[date] = sorted(set(days))
        self._set: frozenset[date] = frozenset(self._days)

    def is_trading_day(self, day: date) -> bool:
        """是否为交易日。"""
        return day in self._set

    def offset(self, day: date, n: int) -> date:
        """day 之后（n 为负则之前）第 n 个交易日；越界抛 ValueError。

        day 不是交易日时，n>0 从其后第一个交易日起算为第 1 个，n<0 向前同理。
        """
        idx = bisect_left(self._days, day)
        if day not in self._set and n > 0:
            idx -= 1
        idx += n
        if not 0 <= idx < len(self._days):
            raise ValueError(f"偏移越界：{day} {n:+d}")
        return self._days[idx]

    def between(self, start: date, end: date) -> list[date]:
        """闭区间 [start, end] 内的交易日。"""
        return self._days[bisect_left(self._days, start) : bisect_right(self._days, end)]

    def count_between(self, start: date, end: date) -> int:
        """半开区间 (start, end] 内的交易日数。"""
        return bisect_right(self._days, end) - bisect_right(self._days, start)
