"""历史行情完整性扫描（scan_history）测试。"""

from datetime import date
from pathlib import Path

import pandas as pd

from qt.calendar import TradingCalendar
from qt.data.check import ANOMALY_COLUMNS, scan_history
from qt.data.store import ParquetStore
from tests.fakes import make_bars, make_securities, populate_store, weekdays

DAYS = weekdays(date(2024, 1, 1), 20)
CAL = TradingCalendar(DAYS)


def _found(frame: pd.DataFrame) -> set[tuple[str, date, str]]:
    return {
        (str(s), pd.Timestamp(d).date(), str(k))
        for s, d, k in zip(frame["symbol"], frame["day"], frame["kind"], strict=True)
    }


def test_scan_flags_every_kind_of_bad_row(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bad = make_bars("600000", DAYS[:12], [10.0] * 12)
    bad.loc[1, "open"] = 0.0
    bad.loc[2, "close"] = -1.0
    bad.loc[3, "high"] = 9.0
    bad.loc[3, "low"] = 11.0
    bad.loc[4, "adj_factor"] = 0.0
    bad.loc[8:, "close"] = 13.0  # 第 9 根日线复权收盘价 +30%
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", date(2010, 1, 1), None)]),
        bars=[bad],
    )
    found = scan_history(store, CAL)
    assert list(found.columns) == ANOMALY_COLUMNS
    assert _found(found) == {
        ("600000", DAYS[1], "non_positive_open"),
        ("600000", DAYS[2], "non_positive_close"),
        ("600000", DAYS[3], "high_below_low"),
        ("600000", DAYS[4], "non_positive_adj_factor"),
        ("600000", DAYS[8], "price_jump"),
    }
    jump = found[found["kind"] == "price_jump"]["detail"].iloc[0]
    assert "+30.0%" in jump


def test_scan_ignores_ex_dividend_gaps_suspensions_and_new_listings(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    # 除权：未复权价 10 → 5，复权因子 1 → 2，复权价不变
    ex = make_bars("600001", DAYS[:6], [10.0, 10.0, 10.0, 5.0, 5.0, 5.0], [1, 1, 1, 2, 2, 2])
    # 停牌后复牌：DAYS[2] 之后直到 DAYS[6] 才有日线，复牌涨 50% 不报
    gap = make_bars("600002", [DAYS[0], DAYS[1], DAYS[2], DAYS[6]], [10.0, 10.0, 10.0, 15.0])
    # 新股：DAYS[0] 上市，前 5 个交易日（含上市日）不查，第 6 个交易日起查
    ipo = make_bars("600003", DAYS[:7], [10.0, 14.0, 19.0, 25.0, 33.0, 43.0, 56.0])
    populate_store(
        store,
        securities=make_securities(
            [
                ("600001", "sh_main", date(2010, 1, 1), None),
                ("600002", "sh_main", date(2010, 1, 1), None),
                ("600003", "sh_main", DAYS[0], None),
            ]
        ),
        bars=[ex, gap, ipo],
    )
    found = scan_history(store, CAL)
    assert _found(found) == {("600003", DAYS[5], "price_jump"), ("600003", DAYS[6], "price_jump")}


def test_scan_of_empty_store_is_empty(tmp_path: Path) -> None:
    found = scan_history(ParquetStore(tmp_path), CAL)
    assert found.empty
    assert list(found.columns) == ANOMALY_COLUMNS


def test_scan_flags_non_finite_price_and_factor_fields(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bad = make_bars("600000", DAYS[:8], [10.0] * 8)
    bad.loc[1, "open"] = float("nan")
    bad.loc[2, "high"] = float("nan")
    bad.loc[3, "low"] = float("nan")
    bad.loc[4, "close"] = float("inf")
    bad.loc[5, "adj_factor"] = float("nan")
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", date(2010, 1, 1), None)]),
        bars=[bad],
    )
    found = scan_history(store, CAL)
    assert _found(found) == {
        ("600000", DAYS[1], "non_finite_value"),
        ("600000", DAYS[2], "non_finite_value"),
        ("600000", DAYS[3], "non_finite_value"),
        ("600000", DAYS[4], "non_finite_value"),
        ("600000", DAYS[5], "non_finite_value"),
    }
    details = dict(zip(found["day"], found["detail"], strict=True))
    assert "open" in details[pd.Timestamp(DAYS[1])]
    assert "adj_factor" in details[pd.Timestamp(DAYS[5])]
