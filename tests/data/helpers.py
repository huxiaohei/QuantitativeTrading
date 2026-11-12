"""更新与完整性检查测试用的小工具。"""

import os
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from qt.data.schema import Table
from qt.data.store import ParquetStore
from tests.fakes import FakeSource


def make_trade_days(days: list[date]) -> pd.DataFrame:
    """构造 TRADE_DAYS 表。"""
    return pd.DataFrame({"day": pd.to_datetime(days).astype("datetime64[ns]")})


def set_file_age(store_root: Path, table: Table, symbol: str, today: date, age_days: int) -> None:
    """把按股票分文件的表的修改时间改成 today 往前 age_days 天。"""
    path = store_root / table.value / f"{symbol}.parquet"
    stamp = datetime.combine(today - timedelta(days=age_days), time(12, 0)).timestamp()
    os.utime(path, (stamp, stamp))


def bar_days(store: ParquetStore, symbol: str) -> list[date]:
    """某只股票已存储日线的全部日期。"""
    return [ts.date() for ts in store.read(Table.BARS, symbols=[symbol])["day"]]


def set_table_age(store_root: Path, table: Table, today: date, age_days: int) -> None:
    """把不按股票分文件的表的修改时间改成 today 往前 age_days 天。"""
    path = store_root / f"{table.value}.parquet"
    stamp = datetime.combine(today - timedelta(days=age_days), time(12, 0)).timestamp()
    os.utime(path, (stamp, stamp))


class LookupSource(FakeSource):
    """支持逐只查行业的假数据源；industry_calls 记录被查询的股票。

    classes 为 {代码: (行业, 是否金融)}，值为 None 表示没有分类；industry_fail 中的股票查询时抛错。
    """

    def __init__(
        self, classes: Mapping[str, tuple[str, bool] | None], **tables: pd.DataFrame
    ) -> None:
        super().__init__(**tables)
        self._classes = classes
        self.industry_calls: list[str] = []
        self.industry_fail: set[str] = set()
        self.as_of_seen: date | None = None

    def industry(self, symbol: str) -> tuple[str, bool] | None:
        self.industry_calls.append(symbol)
        if symbol in self.industry_fail:
            raise ConnectionError(f"模拟行业查询失败：{symbol}")
        return self._classes.get(symbol)

    def set_as_of(self, as_of: date) -> None:
        self.as_of_seen = as_of


class InterruptingSource(FakeSource):
    """第 interrupt_on 次 bars() 调用时抛 KeyboardInterrupt（模拟 Ctrl-C）。"""

    def __init__(self, interrupt_on: int, **tables: pd.DataFrame) -> None:
        super().__init__(**tables)
        self._interrupt_on = interrupt_on
        self._bars_calls = 0

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self._bars_calls += 1
        if self._bars_calls == self._interrupt_on:
            raise KeyboardInterrupt
        return super().bars(symbol, start, end)


class BrokenBenchmarkSource(FakeSource):
    """benchmark() 总是失败。"""

    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame:
        raise ConnectionError("基准不可用")
