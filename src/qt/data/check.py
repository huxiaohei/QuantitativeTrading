"""历史行情完整性扫描：对已存储的全部日线做一次向量化检查。

qt data update 只检查最新一个交易日；本模块检查全部历史，供 qt data check 使用。
"""

from datetime import date
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from qt.calendar import TradingCalendar
from qt.data.schema import Table
from qt.data.store import ParquetStore

ANOMALY_COLUMNS = ["symbol", "day", "kind", "detail"]
# qt data check 写在数据目录下的明细文件名；回测报告据此显示异常数量
ANOMALY_FILE = "history-anomalies.csv"
# 异常类型 -> 中文说明（命令行与报告共用）
KIND_LABELS: dict[str, str] = {
    "non_positive_open": "开盘价不为正",
    "non_positive_close": "收盘价不为正",
    "high_below_low": "最高价低于最低价",
    "non_positive_adj_factor": "复权因子不为正",
    "price_jump": "复权收盘价单日涨跌超过 21%",
}
# 复权后收盘价单日涨跌幅的上限（与 update.check_integrity 相同）
_MAX_JUMP = 0.21
# 上市后前 N 个交易日（含上市日）不检查涨跌幅
_IPO_EXEMPT_DAYS = 5


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="str"),
            "day": pd.Series(dtype="datetime64[ns]"),
            "kind": pd.Series(dtype="str"),
            "detail": pd.Series(dtype="str"),
        }
    )


def _rows(
    bars: pd.DataFrame, mask: "pd.Series[bool]", kind: str, detail: "pd.Series[str]"
) -> pd.DataFrame:
    hit = bars[mask.to_numpy()]
    return pd.DataFrame(
        {
            "symbol": hit["symbol"].astype("str"),
            "day": hit["day"],
            "kind": pd.Series([kind] * len(hit), index=hit.index, dtype="str"),
            "detail": detail[mask.to_numpy()].astype("str"),
        }
    )


def _calendar_days(calendar: TradingCalendar) -> npt.NDArray[np.datetime64]:
    """日历里全部交易日（升序）。"""
    return np.array(calendar.between(date.min, date.max), dtype="datetime64[ns]")


def _first_trading_index(
    calendar: TradingCalendar, list_dates: "pd.Series[Any]"
) -> "pd.Series[float]":
    """每个上市日对应的"上市后第一个交易日"在日历中的序号；上市日缺失为 NaN。"""
    days = _calendar_days(calendar)
    values = list_dates.to_numpy(dtype="datetime64[ns]")
    index: npt.NDArray[np.float64] = np.asarray(
        cast(Any, np.searchsorted(days, values, side="left")), dtype="float64"
    )
    index[np.isnat(values)] = np.nan
    return pd.Series(index, index=list_dates.index)


def scan_history(store: ParquetStore, calendar: TradingCalendar) -> pd.DataFrame:
    """扫描全部已存储日线，返回异常行（列 symbol, day, kind, detail），按 symbol、day 排序。

    检查：开盘价或收盘价不为正、最高价低于最低价、复权因子不为正，以及相邻两个交易日都有日线时
    复权收盘价（close * adj_factor）单日涨跌幅绝对值超过 21%（停牌复牌后不查；上市后前 5 个
    交易日不查；任一侧价格或因子不为正的不查，已单独报告）。
    """
    bars = store.read(Table.BARS)
    if len(bars) == 0:
        return _empty()
    bars = bars.sort_values(["symbol", "day"], kind="stable").reset_index(drop=True)
    found: list[pd.DataFrame] = []
    for col, kind in (
        ("open", "non_positive_open"),
        ("close", "non_positive_close"),
        ("adj_factor", "non_positive_adj_factor"),
    ):
        mask = bars[col] <= 0
        detail = KIND_LABELS[kind] + "：" + bars[col].map("{:g}".format)
        found.append(_rows(bars, mask, kind, detail))
    low_high = bars["high"] < bars["low"]
    detail = (
        "最高价 "
        + bars["high"].map("{:g}".format)
        + " 低于最低价 "
        + bars["low"].map("{:g}".format)
    )
    found.append(_rows(bars, low_high, "high_below_low", detail))

    # 相邻交易日的复权收盘价跳变
    days = _calendar_days(calendar)
    stamp = bars["day"].to_numpy(dtype="datetime64[ns]")
    pos: npt.NDArray[np.int64] = np.asarray(
        cast(Any, np.searchsorted(days, stamp, side="left")), dtype="int64"
    )
    clipped: npt.NDArray[np.int64] = np.minimum(pos, len(days) - 1)
    in_calendar: npt.NDArray[np.bool_] = (pos < len(days)) & (days[clipped] == stamp)
    # 不在日历里的日子给一个不可能相邻的序号
    position = pd.Series(np.where(in_calendar, pos, -10).astype("float64"))
    hfq = bars["close"] * bars["adj_factor"]
    grouped = bars.groupby("symbol", sort=False)
    prev_hfq = grouped["close"].shift(1) * grouped["adj_factor"].shift(1)
    prev_pos = position.groupby(bars["symbol"], sort=False).shift(1)
    consecutive = (position - prev_pos == 1) & in_calendar
    valid = consecutive & (hfq > 0) & (prev_hfq > 0)
    change = pd.Series(np.nan, index=bars.index)
    change[valid] = hfq[valid] / prev_hfq[valid] - 1

    secs = store.read(Table.SECURITIES).set_index("symbol")
    first = _first_trading_index(calendar, secs["list_date"])
    exempt_until = bars["symbol"].map(first) + (_IPO_EXEMPT_DAYS - 1)
    exempt = position <= exempt_until  # 上市日缺失（NaN）时比较为假，不豁免
    jump = (change.abs() > _MAX_JUMP) & ~exempt
    detail = "复权收盘价单日变动 " + change.map("{:+.1%}".format)
    found.append(_rows(bars, jump, "price_jump", detail))

    out = pd.concat(found, ignore_index=True)
    return out.sort_values(["symbol", "day", "kind"], kind="stable").reset_index(drop=True)[
        ANOMALY_COLUMNS
    ]
