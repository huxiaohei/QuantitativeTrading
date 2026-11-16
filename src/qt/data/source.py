"""数据源协议：真实数据源（AKShare）与测试假数据源都实现它。"""

from datetime import date
from typing import Protocol

import pandas as pd


class DataSource(Protocol):
    """返回的 DataFrame 列与类型须符合 schema 中对应表的定义。"""

    def trade_days(self) -> pd.DataFrame: ...

    def securities(self) -> pd.DataFrame: ...

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...

    def daily_snapshot(self) -> pd.DataFrame:
        """全市场最新一个交易日的批量行情，列见 schema.SNAPSHOT_SCHEMA；没有日期列。

        只请求一次、失败不重试（避免加重限流）；失败抛异常，调用方退回逐只请求。
        """
        ...

    def dividends(self, symbol: str) -> pd.DataFrame: ...

    def financials(self, symbol: str) -> pd.DataFrame: ...

    def industries(self) -> pd.DataFrame: ...

    def st_history(self) -> pd.DataFrame: ...

    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame: ...
