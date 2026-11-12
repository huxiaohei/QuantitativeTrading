"""更新与完整性检查测试用的小工具。"""

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from qt.data.schema import Table
from qt.data.store import ParquetStore


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
