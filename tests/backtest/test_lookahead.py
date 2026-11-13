"""未来函数检验：把数据截断在某一天 D 之后，D 及以前的一切结果必须完全相同。

专用数据集（不复用 populate_trading_store，以免影响其他测试）：
    - 价格在折线走势上叠加每日 ±1.5% 的确定性波动，D 之后的收盘价若被提前看到，
      会改变价格分位、止损与止盈；
    - 每个截断日 D：600005 在 D 当天公告一笔当前年度分红（股息率由 3.5% 升到 6%），
      600006 在 D 之后 5 个交易日公告同样的分红；600001、600003 在 D 当天公告一份
      净利润同比 -50% 的季报（触发基本面止损），600002、600004 在 D 之后 5 个交易日公告；
      600001 至 600004 在 D 之前 5 天公告、D 之后 3 天除权一笔 10 转 5 的纯送转；
    - 600005 在第一个截断日之后被 ST（未结束的区间）。
截断规则：日线删除 day > D；分红与财务删除 announce_date >= D（D 当天公告的只在完整数据里，
正确的规则下两边在 D 当天都看不到）；除权日晚于 D 的送转去掉（纯送转行整行删除）；
基准删除 day > D；退市日晚于 D 的置空；ST 区间起点晚于 D 的删除。
"""

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from qt.backtest.engine import BacktestResult
from qt.backtest.report import run_period
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.data.update import load_calendar
from qt.features import FeaturePanel, build_panel
from tests.fakes import (
    default_app_config,
    make_bars,
    make_benchmark,
    make_dividends,
    make_financials,
    make_industries,
    make_securities,
    make_st_history,
    populate_store,
    weekdays,
    write_calendar,
)

START = date(2021, 1, 4)
N_DAYS = 520
# 截断日在交易日序列中的位置：第一个在持仓期间，第二个在样本外暴跌之前
CUTOFFS = (290, 405)
_KNOTS: dict[str, list[tuple[int, float]]] = {
    "600001": [(0, 14.0), (199, 14.0), (259, 10.0), (262, 10.0), (330, 12.5), (519, 12.5)],
    "600002": [(0, 14.0), (199, 14.0), (259, 10.0), (262, 10.0), (290, 8.0), (519, 8.0)],
    "600003": [(0, 14.0), (199, 14.0), (259, 10.0), (399, 10.0), (430, 7.0), (519, 7.0)],
    "600004": [(0, 14.0), (199, 14.0), (259, 10.2), (399, 10.2), (430, 7.0), (519, 7.0)],
    "600005": [(0, 14.0), (199, 14.0), (259, 10.0), (519, 10.0)],
    "600006": [(0, 14.0), (199, 14.0), (259, 10.0), (519, 10.0)],
}
_TRADED = ("600001", "600002", "600003", "600004")


def _open_year(day: date) -> int:
    """day 当天"当前未关闭财年"Y*+1。"""
    return day.year if day.month >= 9 else day.year - 1


def build_store(root: Path) -> list[date]:
    """写入专用数据集，返回交易日列表。"""
    store = ParquetStore(root)
    days = weekdays(START, N_DAYS)
    write_calendar(store, days)
    symbols = list(_KNOTS)
    bars: list[pd.DataFrame] = []
    for k, (symbol, knots) in enumerate(_KNOTS.items()):
        base = np.interp(range(N_DAYS), [p[0] for p in knots], [p[1] for p in knots])
        wiggle = 1 + 0.015 * np.sin(0.9 * np.arange(N_DAYS) + k)
        closes = [round(float(c), 2) for c in base * wiggle]
        frame = make_bars(symbol, days, closes)
        frame["amount"] = pd.Series([1e8] * N_DAYS, dtype="float64")
        bars.append(frame)

    dividends: dict[str, list[tuple[int, date, date, float, float]]] = {s: [] for s in symbols}
    financials: dict[str, list[dict[str, object]]] = {s: [] for s in symbols}
    for symbol in symbols:
        cash = 0.6 if symbol in _TRADED else 0.35
        for fy in range(2015, 2022):
            dividends[symbol].append((fy, date(fy + 1, 4, 20), date(fy + 1, 6, 15), cash, 0.0))
        for fy in range(2017, 2022):
            financials[symbol].append(
                {"report_period": date(fy, 12, 31), "announce_date": date(fy + 1, 4, 20)}
            )
    for i in CUTOFFS:
        cutoff, later = days[i], days[i + 5]
        for symbol, when in (("600005", cutoff), ("600006", later)):
            dividends[symbol].append((_open_year(when), when, when + timedelta(days=10), 0.6, 0.0))
        period = date(2021, 9, 30) if cutoff < date(2022, 4, 20) else date(2022, 6, 30)
        for symbol, when in (
            ("600001", cutoff),
            ("600003", cutoff),
            ("600002", later),
            ("600004", later),
        ):
            financials[symbol].append(
                {"report_period": period, "announce_date": when, "net_profit_yoy": -0.5}
            )
        for symbol in _TRADED:
            ex = cutoff + timedelta(days=3)
            dividends[symbol].append((_open_year(ex), cutoff - timedelta(days=5), ex, 0.0, 0.5))

    populate_store(
        store,
        securities=make_securities([(s, "sh_main", date(2010, 1, 1), None) for s in symbols]),
        bars=bars,
        dividends=[make_dividends(s, rows) for s, rows in dividends.items()],
        financials=[make_financials(s, rows) for s, rows in financials.items()],
        industries=make_industries([(s, f"行业{k}", False) for k, s in enumerate(symbols)]),
        st_history=make_st_history(
            [
                ("600006", days[50], days[80]),  # 已结束的区间：两边都保留
                ("600005", days[CUTOFFS[0] + 2], None),  # 第一个截断日之后才开始：截断时删除
            ]
        ),
    )
    store.write(
        Table.BENCHMARKS, make_benchmark("H00300", days, [100.0 + i for i in range(N_DAYS)])
    )
    return days


def truncate(store: ParquetStore, root: Path, cutoff: date) -> ParquetStore:
    """复制 store，删掉 cutoff 当天及以后才公告、或 cutoff 之后才发生的信息。"""
    stamp = pd.Timestamp(cutoff)
    out = ParquetStore(root)
    out.write(Table.TRADE_DAYS, store.read(Table.TRADE_DAYS))
    secs = store.read(Table.SECURITIES)
    secs["delist_date"] = secs["delist_date"].where(secs["delist_date"] <= stamp)
    out.write(Table.SECURITIES, secs)
    out.write(Table.INDUSTRIES, store.read(Table.INDUSTRIES))
    st = store.read(Table.ST_HISTORY)
    out.write(Table.ST_HISTORY, st[st["start"] <= stamp])
    bench = store.read(Table.BENCHMARKS)
    out.write(Table.BENCHMARKS, bench[bench["day"] <= stamp])
    for symbol in secs["symbol"]:
        sym = str(symbol)
        bars = store.read(Table.BARS, symbols=[sym])
        out.write(Table.BARS, bars[bars["day"] <= stamp], sym)
        fin = store.read(Table.FINANCIALS, symbols=[sym])
        out.write(Table.FINANCIALS, fin[fin["announce_date"] < stamp], sym)
        div = store.read(Table.DIVIDENDS, symbols=[sym])
        div = div[div["announce_date"] < stamp].copy()
        future_bonus = div["ex_date"] > stamp
        div.loc[future_bonus, "bonus_per_share"] = 0.0
        empty = (div["cash_per_share"] == 0) & (div["bonus_per_share"] == 0)
        out.write(Table.DIVIDENDS, div[~empty], sym)
    return out


def config(days: list[date]) -> AppConfig:
    cfg = default_app_config()
    backtest = cfg.backtest.model_copy(
        update={
            "in_sample_start": days[0],
            "in_sample_end": days[360],
            "out_sample_start": days[361],
        }
    )
    return cfg.model_copy(update={"backtest": backtest})


def _held_on(result: BacktestResult, day: date) -> set[str]:
    held = {t.symbol for t in result.trades if t.entry_day <= day < t.exit_day}
    return held | {p.symbol for p in result.open_positions if p.entry_day <= day}


def _assert_same_up_to(
    full: BacktestResult,
    cut: BacktestResult,
    full_panel: FeaturePanel,
    cut_panel: FeaturePanel,
    days: list[date],
    cutoff: date,
) -> None:
    pd.testing.assert_series_equal(full.equity.loc[:cutoff], cut.equity)
    pd.testing.assert_series_equal(full.cash.loc[:cutoff], cut.cash)
    assert [t for t in full.trades if t.exit_day <= cutoff] == cut.trades
    open_full = [
        (t.symbol, t.entry_day, t.cost) for t in full.trades if t.entry_day <= cutoff < t.exit_day
    ] + [(p.symbol, p.entry_day, p.cost) for p in full.open_positions if p.entry_day <= cutoff]
    assert sorted(open_full) == sorted((p.symbol, p.entry_day, p.cost) for p in cut.open_positions)
    assert [e for e in full.risk_events if e.day <= cutoff] == cut.risk_events
    assert [r for r in full.rejections if r.candidate.day <= cutoff] == cut.rejections
    for day in (d for d in days if d <= cutoff):
        pd.testing.assert_frame_equal(full_panel.snapshot(day), cut_panel.snapshot(day))
        for symbol in sorted(_held_on(cut, day)):
            assert full_panel.bar(day, symbol) == cut_panel.bar(day, symbol), (day, symbol)


def test_results_up_to_a_day_do_not_depend_on_later_data(tmp_path: Path) -> None:
    days = build_store(tmp_path / "full")
    full_store = ParquetStore(tmp_path / "full")
    cfg = config(days)
    full = run_period(cfg, full_store, days[0], days[-1])
    calendar = load_calendar(full_store)
    full_panel = build_panel(full_store, calendar, cfg, days[0], days[-1])
    for i in CUTOFFS:
        cutoff = days[i]
        cut_store = truncate(full_store, tmp_path / f"cut{i}", cutoff)
        cut = run_period(cfg, cut_store, days[0], cutoff)
        assert cut.equity.index[-1] == cutoff
        assert cut.open_positions, cutoff  # 截断日确有持仓
        cut_panel = build_panel(cut_store, calendar, cfg, days[0], cutoff)
        _assert_same_up_to(full, cut, full_panel, cut_panel, days, cutoff)
    assert not math.isnan(float(full.equity.iloc[-1]))
