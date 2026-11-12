"""退市日期定稿：以已存储的日线为准。"""

from typing import Any

import pandas as pd

from qt.data.schema import Table
from qt.data.store import ParquetStore

# 退市表里没有日期时的占位值（datetime64[ns] 能表示的最大日期附近）；
# 定稿时会被“最后一根日线 + 1 天”替换，或连同该证券一起丢弃，绝不会进入回测
UNKNOWN_DELIST_DATE = pd.Timestamp("2262-04-11")


def finalize_securities(secs: pd.DataFrame, store: ParquetStore) -> tuple[pd.DataFrame, list[str]]:
    """按已存储的日线定稿 secs 里全部已退市证券的 delist_date，返回（新表，被丢弃的代码）。

    - 有日线：delist_date = 最后一根日线的日期 + 1 个日历日（回测引擎在其后第一个交易日
      以最后收盘价清仓）；沪市退市表给的是“暂停上市日期”，可能早于退市整理期的日线，
      深市给的是终止上市日期，因此一律不用表里的日期；
    - 没有日线：沿用退市表的日期；表里也没有日期（占位值）则丢弃该证券；
    - 在市证券（delist_date 为空）不动。
    返回的表里绝不含占位值。
    """
    dates: list[Any] = []
    keep: list[bool] = []
    dropped: list[str] = []
    for symbol, when in zip(secs["symbol"], secs["delist_date"], strict=True):
        if pd.isna(when):
            dates.append(pd.NaT)
            keep.append(True)
            continue
        last = store.last_day(Table.BARS, str(symbol))
        if last is not None:
            dates.append(pd.Timestamp(last) + pd.Timedelta(days=1))
            keep.append(True)
        elif when == UNKNOWN_DELIST_DATE:
            dropped.append(str(symbol))
            dates.append(when)
            keep.append(False)
        else:
            dates.append(when)
            keep.append(True)
    out = secs.copy()
    out["delist_date"] = pd.Series(dates, dtype="datetime64[ns]")
    return out[pd.Series(keep, index=out.index, dtype="bool")].reset_index(drop=True), dropped


def finalize_delist_dates(store: ParquetStore) -> list[str]:
    """对已存储的 securities 表做 finalize_securities 并写回，返回被丢弃的代码。"""
    secs = store.read(Table.SECURITIES)
    if len(secs) == 0:
        return []
    out, dropped = finalize_securities(secs, store)
    store.write(Table.SECURITIES, out)
    return dropped
