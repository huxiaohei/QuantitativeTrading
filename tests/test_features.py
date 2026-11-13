"""FeaturePanel 测试：重点验证时点正确（只能看到 day 之前已公告的信息）。"""

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
import pytest

from qt.calendar import TradingCalendar
from qt.data.store import ParquetStore
from qt.features import Bar, FeaturePanel, build_panel
from tests.fakes import (
    default_app_config,
    make_bars,
    make_dividends,
    make_financials,
    make_industries,
    make_securities,
    make_snapshot,
    make_st_history,
    populate_store,
    weekdays,
)

D = weekdays(date(2020, 6, 1), 700)
SYMBOL = "600000"


def _panel(
    tmp_path: Path,
    bars: Sequence[pd.DataFrame],
    *,
    securities: pd.DataFrame | None = None,
    dividends: Sequence[pd.DataFrame] = (),
    financials: Sequence[pd.DataFrame] = (),
    industries: pd.DataFrame | None = None,
    st_history: pd.DataFrame | None = None,
    start: date | None = None,
    end: date | None = None,
) -> FeaturePanel:
    if securities is None:
        securities = make_securities(
            [(str(b["symbol"].iloc[0]), "sh_main", date(2010, 1, 1), None) for b in bars]
        )
    store = ParquetStore(tmp_path)
    populate_store(
        store,
        securities=securities,
        bars=bars,
        dividends=dividends,
        financials=financials,
        industries=industries,
        st_history=st_history,
    )
    return build_panel(store, TradingCalendar(D), default_app_config(), start or D[0], end or D[-1])


def _flat(symbol: str = SYMBOL, price: float = 10.0, n: int | None = None) -> pd.DataFrame:
    n = len(D) if n is None else n
    return make_bars(symbol, D[:n], [price] * n)


def _f(snap: pd.DataFrame, symbol: str, col: str) -> float:
    return float(cast(float, snap.at[symbol, col]))


def _b(snap: pd.DataFrame, symbol: str, col: str) -> bool:
    return bool(cast(bool, snap.at[symbol, col]))


def test_financials_invisible_until_day_after_announcement(tmp_path: Path) -> None:
    fin = make_financials(
        SYMBOL,
        [
            {
                "report_period": date(2020, 9, 30),
                "announce_date": date(2020, 10, 28),
                "net_profit_yoy": 0.2,
                "debt_ratio": 0.4,
            },
            {
                "report_period": date(2020, 12, 31),
                "announce_date": date(2021, 4, 28),
                "net_profit_yoy": 0.3,
                "debt_ratio": 0.6,
            },
        ],
    )
    panel = _panel(tmp_path, [_flat()], financials=[fin])
    assert math.isnan(_f(panel.snapshot(date(2020, 10, 28)), SYMBOL, "profit_yoy"))
    assert _f(panel.snapshot(date(2020, 10, 29)), SYMBOL, "profit_yoy") == 0.2
    assert _f(panel.snapshot(date(2021, 4, 28)), SYMBOL, "profit_yoy") == 0.2
    assert _f(panel.snapshot(date(2021, 4, 28)), SYMBOL, "debt_ratio") == 0.4
    assert _f(panel.snapshot(date(2021, 4, 29)), SYMBOL, "profit_yoy") == 0.3
    assert _f(panel.snapshot(date(2021, 4, 29)), SYMBOL, "debt_ratio") == 0.6


def test_quarterly_report_after_annual_takes_over_profit_yoy(tmp_path: Path) -> None:
    fin = make_financials(
        SYMBOL,
        [
            {
                "report_period": date(2020, 12, 31),
                "announce_date": date(2021, 4, 20),
                "net_profit_yoy": 0.3,
            },
            {
                "report_period": date(2021, 3, 31),
                "announce_date": date(2021, 4, 28),
                "net_profit_yoy": -0.1,
            },
            # 迟到的旧报告（期末更早）不能覆盖更新的一期
            {
                "report_period": date(2020, 9, 30),
                "announce_date": date(2021, 5, 20),
                "net_profit_yoy": 9.9,
            },
        ],
    )
    panel = _panel(tmp_path, [_flat()], financials=[fin])
    assert _f(panel.snapshot(date(2021, 4, 21)), SYMBOL, "profit_yoy") == 0.3
    assert _f(panel.snapshot(date(2021, 4, 28)), SYMBOL, "profit_yoy") == 0.3
    assert _f(panel.snapshot(date(2021, 4, 29)), SYMBOL, "profit_yoy") == -0.1
    assert _f(panel.snapshot(date(2021, 5, 24)), SYMBOL, "profit_yoy") == -0.1


def test_dividend_yield_uses_only_announced_dividends(tmp_path: Path) -> None:
    div = make_dividends(SYMBOL, [(2020, date(2021, 3, 30), date(2021, 6, 1), 0.5)])
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _f(panel.snapshot(date(2021, 3, 30)), SYMBOL, "dividend_yield") == 0.0
    assert _f(panel.snapshot(date(2021, 3, 31)), SYMBOL, "dividend_yield") == pytest.approx(0.05)


def test_dividend_yield_is_larger_of_closed_and_current_fiscal_year(tmp_path: Path) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2019, date(2020, 7, 1), date(2020, 8, 1), 0.3),
            (2020, date(2021, 3, 30), date(2021, 6, 1), 0.5),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    # 2021 年 7 月：Y*=2019（0.3），当前年度 2020 已公告 0.5，取较大者，两年不相加
    assert _f(panel.snapshot(date(2021, 7, 1)), SYMBOL, "dividend_yield") == pytest.approx(0.05)
    assert _f(panel.snapshot(date(2021, 7, 2)), SYMBOL, "dividend_yield") == pytest.approx(0.05)
    assert _f(panel.snapshot(date(2021, 9, 1)), SYMBOL, "dividend_yield") == pytest.approx(0.05)
    assert _f(panel.snapshot(date(2020, 7, 1)), SYMBOL, "dividend_yield") == 0.0  # 公告当天
    assert _f(panel.snapshot(date(2020, 7, 2)), SYMBOL, "dividend_yield") == pytest.approx(0.03)


def _yield(panel: FeaturePanel, day: date, symbol: str = SYMBOL) -> float:
    return _f(panel.snapshot(day), symbol, "dividend_yield")


def test_announcement_drift_does_not_double_count(tmp_path: Path) -> None:
    """评审场景 A：今年的实施公告比去年早 20 天，旧口径下两年分红在窗口里叠加。"""
    div = make_dividends(
        SYMBOL,
        [
            (2019, date(2020, 7, 10), date(2020, 7, 17), 0.30),
            (2020, date(2021, 6, 20), date(2021, 6, 28), 0.30),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _yield(panel, date(2020, 7, 13)) == pytest.approx(0.03)
    assert _yield(panel, date(2021, 6, 21)) == pytest.approx(0.03)
    assert _yield(panel, date(2021, 7, 12)) == pytest.approx(0.03)
    for day in D:
        assert _yield(panel, day) <= 0.03 + 1e-12, day


def _scenario_b(tmp_path: Path) -> FeaturePanel:
    """评审场景 B：2020-07-17 实施 10 派 5 + 10 转 10，价格 20 → 10；
    2020 财年每股（新股）派 0.30。"""
    split = date(2020, 7, 17)
    closes = [20.0 if d < split else 10.0 for d in D]
    factors = [1.0 if d < split else 2.05 for d in D]
    div = make_dividends(
        SYMBOL,
        [
            (2018, date(2019, 7, 10), date(2019, 7, 17), 0.50),
            (2019, date(2020, 7, 10), date(2020, 7, 17), 0.50, 1.0),
            (2020, date(2021, 7, 10), date(2021, 7, 17), 0.30),
        ],
    )
    return _panel(tmp_path, [make_bars(SYMBOL, D, closes, factors)], dividends=[div])


def test_bonus_shares_rescale_past_dividends_to_current_share_count(tmp_path: Path) -> None:
    panel = _scenario_b(tmp_path)
    assert _yield(panel, date(2020, 7, 16)) == pytest.approx(0.025)  # 除权前：0.50 / 20
    assert _yield(panel, date(2020, 7, 17)) == pytest.approx(0.025)  # 除权日：0.25 / 10
    assert _yield(panel, date(2020, 7, 20)) == pytest.approx(0.025)
    assert _yield(panel, date(2021, 6, 1)) == pytest.approx(0.025)
    assert not _b(panel.snapshot(date(2020, 7, 20)), SYMBOL, "dividend_cut")
    sept = panel.snapshot(date(2020, 9, 1))  # Y*=2019：0.25 对 2018 年的 0.25
    assert not _b(sept, SYMBOL, "dividend_cut")
    assert _f(sept, SYMBOL, "dividend_fy_total") == pytest.approx(0.25)
    assert _f(sept, SYMBOL, "dividend_prev_fy_total") == pytest.approx(0.25)
    snap = panel.snapshot(date(2021, 9, 1))  # 每股（新股）0.30 是加息，不是削减
    assert not _b(snap, SYMBOL, "dividend_cut")
    assert _f(snap, SYMBOL, "dividend_fy_total") == pytest.approx(0.30)
    assert _f(snap, SYMBOL, "dividend_prev_fy_total") == pytest.approx(0.25)
    assert _f(snap, SYMBOL, "dividend_yield") == pytest.approx(0.03)
    assert int(cast(int, snap.at[SYMBOL, "dividend_years"])) == 3


def test_bonus_with_ex_date_after_day_is_not_applied(tmp_path: Path) -> None:
    panel = _scenario_b(tmp_path)
    # 2020-07-10 公告、07-17 除权：除权前价格仍是旧股本口径，送转不能提前生效
    before = panel.snapshot(date(2020, 7, 16))
    assert _f(before, SYMBOL, "dividend_fy_total") == pytest.approx(0.50)  # Y*=2018
    after = panel.snapshot(date(2020, 7, 17))
    assert _f(after, SYMBOL, "dividend_fy_total") == pytest.approx(0.25)


def test_bonus_only_row_rescales_without_adding_cash(tmp_path: Path) -> None:
    ex = date(2020, 10, 15)
    closes = [10.0 if d < ex else 8.0 for d in D]
    div = make_dividends(
        SYMBOL,
        [
            (2019, date(2020, 3, 30), date(2020, 6, 1), 0.5),
            (2020, date(2020, 9, 30), ex, 0.0, 0.25),  # 纯送转：10 送 2.5
        ],
    )
    panel = _panel(tmp_path, [make_bars(SYMBOL, D, closes)], dividends=[div])
    day_before = panel.snapshot(date(2020, 10, 14))
    assert _f(day_before, SYMBOL, "dividend_fy_total") == pytest.approx(0.5)
    assert _f(day_before, SYMBOL, "dividend_yield") == pytest.approx(0.05)
    on_ex = panel.snapshot(ex)
    assert _f(on_ex, SYMBOL, "dividend_fy_total") == pytest.approx(0.4)
    assert _f(on_ex, SYMBOL, "dividend_yield") == pytest.approx(0.05)
    # 2020 财年只有送转、没有现金：关闭后视为停止分红
    closed = panel.snapshot(date(2021, 9, 1))
    assert _f(closed, SYMBOL, "dividend_fy_total") == 0.0
    assert _f(closed, SYMBOL, "dividend_prev_fy_total") == pytest.approx(0.4)
    assert _b(closed, SYMBOL, "dividend_cut")


def test_interim_dividend_raises_yield_only_when_it_exceeds_closed_year(
    tmp_path: Path,
) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2020, date(2021, 3, 30), date(2021, 6, 1), 0.3),
            (2021, date(2021, 8, 20), date(2021, 9, 15), 0.2),  # 2021 中期
            (2022, date(2022, 8, 20), date(2022, 9, 15), 0.5),  # 2022 中期
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _yield(panel, date(2021, 9, 1)) == pytest.approx(0.03)  # 0.3 对 0.2
    assert _yield(panel, date(2022, 3, 1)) == pytest.approx(0.03)
    assert _yield(panel, date(2022, 8, 22)) == pytest.approx(0.03)  # 2022 年尚不是当前年度
    assert _yield(panel, date(2022, 9, 1)) == pytest.approx(0.05)  # Y*=2021 0.2 对 0.5


def test_price_position_basic_and_flat(tmp_path: Path) -> None:
    n = 300
    closes = [10.0 + i for i in range(n)]
    rising = make_bars(SYMBOL, D[:n], closes)
    panel = _panel(tmp_path, [rising, _flat("600001", n=n)])
    assert math.isnan(_f(panel.snapshot(D[248]), SYMBOL, "price_position"))
    assert _f(panel.snapshot(D[249]), SYMBOL, "price_position") == 1.0
    # 窗口 [D[50], D[299]]，最低 60，最高 309，当前 309 → 1；回落后再验证一个中间值
    snap = panel.snapshot(D[299])
    assert _f(snap, SYMBOL, "price_position") == 1.0
    # 最高等于最低：空
    assert math.isnan(_f(snap, "600001", "price_position"))


def test_price_position_unaffected_by_ex_dividend_gap(tmp_path: Path) -> None:
    n = 400
    ex = 320
    adj = [10.0 + 0.01 * i + (0.5 if i % 7 == 0 else 0.0) for i in range(n)]
    factors = [1.0 if i < ex else 10.0 / 7.0 for i in range(n)]
    raw = [a / f for a, f in zip(adj, factors, strict=True)]
    # 除权日未复权价从约 13 跳到约 9，后复权价平稳
    assert raw[ex] < 0.75 * raw[ex - 1]
    bars = make_bars(SYMBOL, D[:n], raw, factors)
    panel = _panel(tmp_path, [bars])
    adj_arr = np.array(adj) * 1.0
    for i in (ex - 1, ex, ex + 1):
        window = adj_arr[i - 249 : i + 1]
        expected = (adj_arr[i] - window.min()) / (window.max() - window.min())
        assert _f(panel.snapshot(D[i]), SYMBOL, "price_position") == pytest.approx(expected)


def test_price_position_nan_when_history_short(tmp_path: Path) -> None:
    panel = _panel(tmp_path, [_flat(n=100)])
    snap = panel.snapshot(D[99])
    assert math.isnan(_f(snap, SYMBOL, "price_position"))
    # 缺财务、缺分红也只是空值或 0，不报错
    for col in ("roe_avg3", "payout_ratio", "profit_yoy", "debt_ratio", "avg_amount_60d"):
        if col == "avg_amount_60d":
            assert not math.isnan(_f(snap, SYMBOL, col))
        else:
            assert math.isnan(_f(snap, SYMBOL, col))
    assert _f(snap, SYMBOL, "dividend_yield") == 0.0
    assert int(cast(int, snap.at[SYMBOL, "dividend_years"])) == 0
    assert not _b(snap, SYMBOL, "dividend_cut")


def test_suspended_symbol_absent_from_snapshot_and_bar_is_none(tmp_path: Path) -> None:
    other = _flat("600001")
    full = _flat(SYMBOL)
    suspended_day = date(2021, 5, 10)
    bars = full[full["day"] != pd.Timestamp(suspended_day)].reset_index(drop=True)
    panel = _panel(tmp_path, [bars, other])
    snap = panel.snapshot(suspended_day)
    assert SYMBOL not in snap.index
    assert "600001" in snap.index
    assert panel.bar(suspended_day, SYMBOL) is None
    assert panel.last_close(suspended_day, SYMBOL) == 10.0
    assert panel.bar(date(2021, 5, 11), SYMBOL) is not None


def test_last_close_is_latest_on_or_before_day(tmp_path: Path) -> None:
    n = 50
    bars = make_bars(SYMBOL, D[10:n], [float(i) for i in range(10, n)])
    panel = _panel(tmp_path, [bars])
    assert panel.last_close(D[20], SYMBOL) == 20.0
    assert panel.last_close(D[200], SYMBOL) == float(n - 1)
    with pytest.raises(KeyError):
        panel.last_close(D[5], SYMBOL)
    with pytest.raises(KeyError):
        panel.last_close(D[20], "999999")


def test_bar_prev_close_adjusted_on_ex_date(tmp_path: Path) -> None:
    bars = make_bars(SYMBOL, D[:3], [10.0, 10.0, 8.0], [1.0, 1.0, 1.25])
    panel = _panel(tmp_path, [bars], st_history=make_st_history([(SYMBOL, D[2], None)]))
    first = panel.bar(D[0], SYMBOL)
    assert first == Bar(open=10.0, close=10.0, prev_close=10.0, factor_ratio=1.0, is_st=False)
    ex_day = panel.bar(D[2], SYMBOL)
    assert ex_day == Bar(open=8.0, close=8.0, prev_close=8.0, factor_ratio=1.25, is_st=True)
    assert panel.bar(D[1], SYMBOL) == Bar(10.0, 10.0, 10.0, 1.0, False)
    assert panel.bar(D[3], SYMBOL) is None


def test_bar_prev_close_uses_history_before_start(tmp_path: Path) -> None:
    closes = [10.0 + i for i in range(10)]
    panel = _panel(tmp_path, [make_bars(SYMBOL, D[:10], closes)], start=D[5], end=D[9])
    bar = panel.bar(D[5], SYMBOL)
    assert bar is not None
    assert bar.prev_close == 14.0
    assert panel.bar(D[4], SYMBOL) is None
    assert panel.snapshot(D[4]).empty
    assert panel.last_close(D[7], SYMBOL) == 17.0


def test_dividend_cut_and_consecutive_years(tmp_path: Path) -> None:
    cash = {2016: 0.5, 2017: 0.5, 2018: 0.5, 2019: 0.5, 2020: 0.3}
    div = make_dividends(
        SYMBOL, [(fy, date(fy + 1, 3, 30), date(fy + 1, 6, 1), c) for fy, c in cash.items()]
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    # 2021-08-31 之前 2020 财年尚未关闭：只数到 2019，4 年，且无下调
    before = panel.snapshot(date(2021, 3, 31))
    assert int(cast(int, before.at[SYMBOL, "dividend_years"])) == 4
    assert not _b(before, SYMBOL, "dividend_cut")
    snap = panel.snapshot(date(2021, 9, 1))
    assert int(cast(int, snap.at[SYMBOL, "dividend_years"])) == 5
    assert _b(snap, SYMBOL, "dividend_cut")


def _cut_state(panel: FeaturePanel, day: date) -> tuple[bool, float, float, float]:
    """(dividend_cut, total(Y*), total(Y*-1), total(Y*+1))。"""
    snap = panel.snapshot(day)
    return (
        _b(snap, SYMBOL, "dividend_cut"),
        _f(snap, SYMBOL, "dividend_fy_total"),
        _f(snap, SYMBOL, "dividend_prev_fy_total"),
        _f(snap, SYMBOL, "dividend_open_fy_total"),
    )


def test_cut_cleared_once_current_year_announced_dividends_reach_earlier_level(
    tmp_path: Path,
) -> None:
    # 真实案例 600398：FY2014 0.38、FY2015 0.33、FY2016 0.49
    days = weekdays(date(2016, 1, 4), 500)
    div = make_dividends(
        SYMBOL,
        [
            (2013, date(2014, 5, 15), date(2014, 6, 16), 0.19),
            (2014, date(2015, 4, 24), date(2015, 6, 15), 0.38),
            (2015, date(2016, 5, 25), date(2016, 6, 20), 0.33),
            (2016, date(2017, 4, 13), date(2017, 6, 15), 0.49),
        ],
    )
    bars = make_bars(SYMBOL, days, [10.0] * len(days))
    panel = _panel(tmp_path, [bars], dividends=[div], start=days[0], end=days[-1])
    approx = pytest.approx
    # Y*=2014：0.38 对 FY2013 的 0.19，不是下调
    assert _cut_state(panel, date(2016, 8, 31)) == (False, approx(0.38), approx(0.19), approx(0.33))
    # Y*=2015：当前水平 max(0.33, 0) 低于 0.38
    assert _cut_state(panel, date(2016, 9, 1)) == (True, approx(0.33), approx(0.38), 0.0)
    # FY2016 公告当天不可见
    assert _cut_state(panel, date(2017, 4, 13)) == (True, approx(0.33), approx(0.38), 0.0)
    # 次日起当前水平 max(0.33, 0.49) = 0.49，不低于 0.38
    assert _cut_state(panel, date(2017, 4, 14)) == (
        False,
        approx(0.33),
        approx(0.38),
        approx(0.49),
    )
    observed = panel.snapshot(date(2017, 5, 2))
    assert not _b(observed, SYMBOL, "dividend_cut")
    assert _f(observed, SYMBOL, "dividend_yield") == approx(0.049)
    # Y*=2016：0.49 对上一年度 0.33
    assert _cut_state(panel, date(2017, 9, 1)) == (False, approx(0.49), approx(0.33), 0.0)


def test_sustained_cut_stays_until_reduced_year_becomes_previous_year(tmp_path: Path) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2018, date(2019, 5, 15), date(2019, 6, 17), 0.50),
            (2019, date(2020, 5, 15), date(2020, 6, 15), 0.30),
            (2020, date(2021, 5, 14), date(2021, 6, 15), 0.30),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    approx = pytest.approx
    assert _cut_state(panel, date(2020, 9, 1)) == (True, approx(0.30), approx(0.50), 0.0)
    assert _cut_state(panel, date(2021, 5, 14)) == (True, approx(0.30), approx(0.50), 0.0)
    # FY2020 的 0.30 已公告：当前水平 max(0.30, 0.30) 仍低于 0.50
    assert _cut_state(panel, date(2021, 5, 17)) == (
        True,
        approx(0.30),
        approx(0.50),
        approx(0.30),
    )
    assert _cut_state(panel, date(2021, 8, 31))[0]
    # Y*=2020：0.30 对 FY2019 的 0.30
    assert _cut_state(panel, date(2021, 9, 1)) == (False, approx(0.30), approx(0.30), 0.0)


def test_dividend_years_run_broken_by_gap_or_zero_cash(tmp_path: Path) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2016, date(2017, 3, 30), date(2017, 6, 1), 0.5),
            (2017, date(2018, 3, 30), date(2018, 6, 1), 0.5),
            # 2018 年无分红（缺失），2019、2020 恢复
            (2019, date(2020, 3, 30), date(2020, 6, 1), 0.5),
            (2020, date(2021, 3, 30), date(2021, 6, 1), 0.6),
            # 同一财年多笔（中期 + 期末）合并计算
            (2020, date(2021, 3, 30), date(2021, 7, 1), 0.1),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    snap = panel.snapshot(date(2021, 9, 1))
    assert int(cast(int, snap.at[SYMBOL, "dividend_years"])) == 2
    assert not _b(snap, SYMBOL, "dividend_cut")  # 0.7 > 0.5
    assert _f(panel.snapshot(date(2021, 4, 1)), SYMBOL, "dividend_yield") == pytest.approx(0.07)


def test_roe_avg3_needs_three_announced_annual_reports(tmp_path: Path) -> None:
    def annual(year: int, roe: float) -> dict[str, object]:
        return {
            "report_period": date(year, 12, 31),
            "announce_date": date(year + 1, 4, 20),
            "roe": roe,
        }

    fin = make_financials(
        SYMBOL,
        [
            annual(2018, 0.2),
            annual(2019, 0.3),
            annual(2020, 0.1),
            annual(2021, 0.9),
            # 季报不计入
            {
                "report_period": date(2020, 9, 30),
                "announce_date": date(2020, 10, 28),
                "roe": 5.0,
            },
        ],
    )
    panel = _panel(tmp_path, [_flat()], financials=[fin])
    assert math.isnan(_f(panel.snapshot(date(2021, 4, 20)), SYMBOL, "roe_avg3"))
    assert _f(panel.snapshot(date(2021, 4, 21)), SYMBOL, "roe_avg3") == pytest.approx(0.2)
    assert _f(panel.snapshot(date(2022, 4, 20)), SYMBOL, "roe_avg3") == pytest.approx(0.2)
    assert _f(panel.snapshot(date(2022, 4, 21)), SYMBOL, "roe_avg3") == pytest.approx(1.3 / 3)


def test_payout_ratio(tmp_path: Path) -> None:
    def case(symbol: str, eps: float) -> tuple[pd.DataFrame, pd.DataFrame]:
        div = make_dividends(symbol, [(2020, date(2021, 3, 30), date(2021, 6, 1), 0.5)])
        fin = make_financials(
            symbol,
            [
                {
                    "report_period": date(2020, 12, 31),
                    "announce_date": date(2021, 10, 15),
                    "eps": eps,
                }
            ],
        )
        return div, fin

    a_div, a_fin = case("600000", 1.0)
    b_div, b_fin = case("600001", -0.5)
    c_div, c_fin = case("600002", 0.0)
    panel = _panel(
        tmp_path,
        [_flat("600000"), _flat("600001"), _flat("600002")],
        dividends=[a_div, b_div, c_div],
        financials=[a_fin, b_fin, c_fin],
    )
    # 年报尚未公告：空
    for day in (date(2021, 9, 1), date(2021, 10, 15)):
        assert math.isnan(_f(panel.snapshot(day), "600000", "payout_ratio"))
    after = panel.snapshot(date(2021, 10, 18))
    assert _f(after, "600000", "payout_ratio") == pytest.approx(0.5)
    assert math.isnan(_f(after, "600001", "payout_ratio"))
    assert math.isnan(_f(after, "600002", "payout_ratio"))


def test_payout_ratio_needs_annual_report_of_the_dividend_fiscal_year(tmp_path: Path) -> None:
    div = make_dividends(SYMBOL, [(2020, date(2021, 3, 30), date(2021, 6, 1), 0.5)])
    fin = make_financials(
        SYMBOL,
        [
            {"report_period": date(2019, 12, 31), "announce_date": date(2020, 4, 20), "eps": 1.0},
            {"report_period": date(2020, 9, 30), "announce_date": date(2020, 10, 28), "eps": 0.8},
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div], financials=[fin])
    assert math.isnan(_f(panel.snapshot(date(2021, 9, 1)), SYMBOL, "payout_ratio"))


def test_avg_amount_60d(tmp_path: Path) -> None:
    n = 100
    closes = [10.0 + i for i in range(n)]
    panel = _panel(tmp_path, [make_bars(SYMBOL, D[:n], closes)])
    assert math.isnan(_f(panel.snapshot(D[58]), SYMBOL, "avg_amount_60d"))
    expected = sum(closes[11:71]) / 60 * 1_000_000
    assert _f(panel.snapshot(D[70]), SYMBOL, "avg_amount_60d") == pytest.approx(expected)


def test_is_st_interval_boundaries(tmp_path: Path) -> None:
    st = make_st_history(
        [
            (SYMBOL, date(2020, 8, 10), date(2020, 9, 10)),
            (SYMBOL, date(2021, 12, 1), None),
        ]
    )
    panel = _panel(tmp_path, [_flat(), _flat("600001")], st_history=st)
    expected = {
        date(2020, 8, 7): False,
        date(2020, 8, 10): True,
        date(2020, 9, 10): True,
        date(2020, 9, 11): False,
        date(2021, 11, 30): False,
        date(2021, 12, 1): True,
        date(2022, 3, 1): True,
    }
    for day, flag in expected.items():
        snap = panel.snapshot(day)
        assert _b(snap, SYMBOL, "is_st") is flag, day
        assert not _b(snap, "600001", "is_st")
        bar = panel.bar(day, SYMBOL)
        assert bar is not None
        assert bar.is_st is flag


def test_delisted_on(tmp_path: Path) -> None:
    delist = date(2021, 5, 10)
    saturday = date(2021, 5, 15)
    sec = make_securities(
        [
            ("600002", "sh_main", date(2010, 1, 1), delist),
            ("600001", "sh_main", date(2010, 1, 1), delist),
            ("600000", "sh_main", date(2010, 1, 1), None),
            ("600003", "sh_main", date(2010, 1, 1), saturday),
        ]
    )
    bars = [_flat(s) for s in ("600000", "600001", "600002", "600003")]
    panel = _panel(tmp_path, bars, securities=sec)
    assert panel.delisted_on(delist) == ["600001", "600002"]
    assert panel.delisted_on(date(2021, 5, 11)) == []
    assert panel.delisted_on(saturday) == ["600003"]  # 非交易日也如实返回


def test_industry_listed_years_and_missing_industry(tmp_path: Path) -> None:
    ind = make_industries([(SYMBOL, "银行", True)])
    panel = _panel(tmp_path, [_flat(), _flat("600001")], industries=ind)
    day = date(2021, 4, 1)
    snap = panel.snapshot(day)
    assert snap.at[SYMBOL, "industry"] == "银行"
    assert _b(snap, SYMBOL, "is_financial")
    assert snap.at["600001", "industry"] == ""
    assert not _b(snap, "600001", "is_financial")
    assert _f(snap, SYMBOL, "listed_years") == pytest.approx((day - date(2010, 1, 1)).days / 365.25)


def test_board_outside_universe_excluded(tmp_path: Path) -> None:
    sec = make_securities(
        [
            (SYMBOL, "sh_main", date(2010, 1, 1), None),
            ("300001", "chinext", date(2010, 1, 1), None),
        ]
    )
    panel = _panel(tmp_path, [_flat(), _flat("300001")], securities=sec)
    snap = panel.snapshot(date(2021, 4, 1))
    assert list(snap.index) == [SYMBOL]
    assert panel.bar(date(2021, 4, 1), "300001") is None


def test_snapshot_contract_and_range(tmp_path: Path) -> None:
    panel = _panel(tmp_path, [_flat(), _flat("600001")], start=D[10], end=D[300])
    snap = panel.snapshot(D[100])
    expected = make_snapshot({"x": {}})
    assert list(snap.columns) == list(expected.columns)
    assert snap.index.name == "symbol"
    assert list(snap.index) == [SYMBOL, "600001"]
    for col in expected.columns:
        if col == "industry":
            continue
        assert snap[col].dtype == expected[col].dtype, col
    for empty in (panel.snapshot(D[5]), panel.snapshot(D[301]), panel.snapshot(date(2021, 1, 2))):
        assert empty.empty
        assert list(empty.columns) == list(expected.columns)
        assert empty.index.name == "symbol"


def test_bars_after_end_are_ignored(tmp_path: Path) -> None:
    closes = [10.0 + 0.01 * i if i <= 400 else 1000.0 for i in range(len(D))]
    panel = _panel(tmp_path, [make_bars(SYMBOL, D, closes)], end=D[400])
    assert not panel.snapshot(D[400]).empty
    assert panel.snapshot(D[450]).empty
    assert panel.bar(D[450], SYMBOL) is None
    # 若 end 之后的行泄漏进来，这里会是 1000
    assert panel.last_close(D[450], SYMBOL) == closes[400]


def test_price_position_on_start_day_uses_pre_start_history(tmp_path: Path) -> None:
    n = 400
    adj = [10.0 + 0.01 * i + (0.5 if i % 7 == 0 else 0.0) for i in range(n)]
    panel = _panel(tmp_path, [make_bars(SYMBOL, D[:n], adj)], start=D[300], end=D[399])
    window = np.array(adj[51:301])
    expected = (adj[300] - window.min()) / (window.max() - window.min())
    assert _f(panel.snapshot(D[300]), SYMBOL, "price_position") == pytest.approx(expected)


def _years_cut_payout(panel: FeaturePanel, day: date) -> tuple[int, bool, float]:
    snap = panel.snapshot(day)
    years = int(cast(int, snap.at[SYMBOL, "dividend_years"]))
    return years, _b(snap, SYMBOL, "dividend_cut"), _f(snap, SYMBOL, "payout_ratio")


def test_partially_announced_fiscal_year_does_not_distort_dividend_features(
    tmp_path: Path,
) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2020, date(2021, 3, 30), date(2021, 6, 1), 0.5),
            (2021, date(2021, 8, 20), date(2021, 9, 15), 0.2),  # 2021 财年中期分红
            (2021, date(2022, 3, 30), date(2022, 6, 15), 0.4),  # 2021 财年期末分红
        ],
    )
    fin = make_financials(
        SYMBOL,
        [
            {"report_period": date(2020, 12, 31), "announce_date": date(2021, 3, 25), "eps": 1.0},
            {"report_period": date(2021, 12, 31), "announce_date": date(2022, 3, 20), "eps": 1.0},
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div], financials=[fin])
    nan = math.nan

    def check(day: date, years: int, cut: bool, payout: float) -> None:
        got = _years_cut_payout(panel, day)
        assert got[:2] == (years, cut), day
        if math.isnan(payout):
            assert math.isnan(got[2]), day
        else:
            assert got[2] == pytest.approx(payout), day

    check(date(2021, 8, 23), 0, False, nan)  # Y*=2019，无分红
    check(date(2021, 8, 31), 0, False, nan)
    check(date(2021, 9, 1), 1, False, 0.5)  # Y*=2020
    check(date(2022, 3, 31), 1, False, 0.5)  # 2021 财年已公告但未关闭，不影响
    check(date(2022, 8, 31), 1, False, 0.5)
    check(date(2022, 9, 1), 2, False, 0.6)  # Y*=2021，0.6 > 0.5


def test_dividend_stop_is_a_cut(tmp_path: Path) -> None:
    div = make_dividends(SYMBOL, [(2019, date(2020, 3, 30), date(2020, 6, 1), 0.5)])
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _years_cut_payout(panel, date(2021, 8, 31))[:2] == (1, False)
    assert _years_cut_payout(panel, date(2021, 9, 1))[:2] == (0, True)


def test_fiscal_year_closes_after_august_31(tmp_path: Path) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2019, date(2020, 3, 30), date(2020, 6, 1), 0.5),
            (2020, date(2021, 3, 30), date(2021, 6, 1), 0.3),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _years_cut_payout(panel, date(2021, 8, 31))[:2] == (1, False)  # 最后一个旧口径日
    assert _years_cut_payout(panel, date(2021, 9, 1))[:2] == (2, True)  # 首个新口径日


def test_late_announcement_for_closed_year_counts_from_next_day(tmp_path: Path) -> None:
    div = make_dividends(
        SYMBOL,
        [
            (2019, date(2020, 3, 30), date(2020, 6, 1), 0.4),
            (2020, date(2021, 10, 14), date(2021, 11, 1), 0.5),
        ],
    )
    panel = _panel(tmp_path, [_flat()], dividends=[div])
    assert _years_cut_payout(panel, date(2021, 10, 14))[:2] == (0, True)  # 公告当天不可见
    assert _years_cut_payout(panel, date(2021, 10, 15))[:2] == (2, False)


def test_non_date_arguments_are_rejected(tmp_path: Path) -> None:
    panel = _panel(tmp_path, [_flat()])
    for bad in (datetime(2021, 4, 1), pd.Timestamp("2021-04-01")):
        with pytest.raises(TypeError, match=r"datetime\.date"):
            panel.snapshot(bad)
        with pytest.raises(TypeError, match=r"datetime\.date"):
            panel.bar(bad, SYMBOL)
        with pytest.raises(TypeError, match=r"datetime\.date"):
            panel.last_close(bad, SYMBOL)
        with pytest.raises(TypeError, match=r"datetime\.date"):
            panel.delisted_on(bad)


def _brute_total(rows: Sequence[tuple[int, date, date, float, float]], fy: int, day: date) -> float:
    """按定义逐笔计算 total(fy, day)：公告日 < day，按 day 当天的股本折算。"""
    out = 0.0
    for year, announced, ex, cash, _ in rows:
        if year != fy or announced >= day:
            continue
        factor = 1.0
        for _, _, other_ex, _, bonus in rows:
            if ex <= other_ex <= day:
                factor *= 1 + bonus
        out += cash / factor
    return out


def test_dividend_features_match_brute_force_definition(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    days = D[:650]
    symbols = [f"60000{i}" for i in range(4)]
    divs: dict[str, list[tuple[int, date, date, float, float]]] = {}
    for symbol in symbols:
        ex_days = sorted({days[int(i)] for i in rng.integers(0, len(days), size=8)})
        rows: list[tuple[int, date, date, float, float]] = []
        for ex in ex_days:
            announced = ex - timedelta(days=int(rng.integers(0, 40)))
            cash = float(rng.choice([0.0, 0.1, 0.2, 0.35]))
            bonus = float(rng.choice([0.0, 0.0, 0.3, 1.0]))
            cash = 0.15 if cash == 0 and bonus == 0 else cash
            rows.append((ex.year - 1 if ex.month <= 8 else ex.year, announced, ex, cash, bonus))
        divs[symbol] = rows
    closes = [5 + (i % 37) * 0.1 for i in range(len(days))]
    panel = _panel(
        tmp_path,
        [make_bars(s, days, closes) for s in symbols],
        dividends=[make_dividends(s, divs[s]) for s in symbols],
        start=days[0],
        end=days[-1],
    )
    for k in range(0, len(days), 3):
        day = days[k]
        snap = panel.snapshot(day)
        closed = day.year - (1 if day.month >= 9 else 2)
        for symbol in symbols:
            rows = divs[symbol]
            fy = _brute_total(rows, closed, day)
            prev = _brute_total(rows, closed - 1, day)
            current = _brute_total(rows, closed + 1, day)
            run = 0
            while _brute_total(rows, closed - run, day) > 0:
                run += 1
            assert _f(snap, symbol, "dividend_fy_total") == pytest.approx(fy, abs=1e-12)
            assert _f(snap, symbol, "dividend_prev_fy_total") == pytest.approx(prev, abs=1e-12)
            assert _f(snap, symbol, "dividend_open_fy_total") == pytest.approx(current, abs=1e-12)
            level = max(fy, current)
            assert _f(snap, symbol, "dividend_yield") == pytest.approx(level / closes[k], abs=1e-12)
            assert _b(snap, symbol, "dividend_cut") is (prev > 0 and level < prev - 1e-9)
            assert int(cast(int, snap.at[symbol, "dividend_years"])) == run, (day, symbol)


def test_bars_with_non_positive_open_or_close_are_treated_as_missing(tmp_path: Path) -> None:
    bars = make_bars(SYMBOL, D[:10], [10.0 + i for i in range(10)])
    bars.loc[3, "open"] = 0.0
    bars.loc[5, "close"] = -1.0
    panel = _panel(tmp_path, [bars, _flat("600001", n=10)], start=D[0], end=D[9])
    for i in (3, 5):
        assert SYMBOL not in panel.snapshot(D[i]).index
        assert panel.bar(D[i], SYMBOL) is None
    assert panel.last_close(D[5], SYMBOL) == 14.0  # 跳过收盘价为负的那天
    bar = panel.bar(D[6], SYMBOL)
    assert bar is not None
    assert bar.prev_close == 14.0  # 前一根有效日线


@pytest.mark.parametrize("bonus_first", [True, False])
def test_same_ex_date_bonus_and_cash_rows_do_not_depend_on_row_order(
    tmp_path: Path, bonus_first: bool
) -> None:
    ex = date(2020, 7, 17)
    closes = [20.0 if d < ex else 10.0 for d in D]
    bonus_row = (2019, date(2020, 7, 10), ex, 0.0, 1.0)  # 10 转 10
    cash_row = (2019, date(2020, 7, 10), ex, 0.5, 0.0)  # 同一除权日另一行：10 派 5
    rows = [bonus_row, cash_row] if bonus_first else [cash_row, bonus_row]
    div = make_dividends(SYMBOL, rows)
    panel = _panel(tmp_path, [make_bars(SYMBOL, D, closes)], dividends=[div])
    # 现金按除权前股本发放，同日的送转同样要折算：0.5 / 2 = 0.25 元每（新）股
    sept = panel.snapshot(date(2020, 9, 1))
    assert _f(sept, SYMBOL, "dividend_fy_total") == pytest.approx(0.25)
    assert _f(sept, SYMBOL, "dividend_yield") == pytest.approx(0.025)
    assert _yield(panel, date(2020, 7, 16)) == pytest.approx(0.025)  # 除权前 0.5 / 20
