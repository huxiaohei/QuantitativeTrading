"""增量更新、完整性检查与命令行测试（不联网）。"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.delist import UNKNOWN_DELIST_DATE
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.data.update import (
    DELIST_MISSING_PREFIX,
    INDUSTRY_FAILED_PREFIX,
    NO_BARS_PREFIX,
    DataIntegrityError,
    SharedTableError,
    check_integrity,
    count_no_bars,
    fundamentals_due,
    latest_closed_day,
    load_calendar,
    merge_st_history,
    refresh_fundamentals,
    update_all,
)
from tests.data.helpers import (
    LookupSource,
    bar_days,
    make_trade_days,
    set_file_age,
    set_table_age,
)
from tests.fakes import (
    FakeSource,
    default_app_config,
    make_bars,
    make_dividends,
    make_financials,
    make_industries,
    make_securities,
    make_st_history,
    populate_store,
    weekdays,
)

CFG: AppConfig = default_app_config()
DAYS = weekdays(date(2024, 1, 1), 10)  # 2024-01-01 至 2024-01-12
TODAY = DAYS[-1]
LISTED = date(2000, 1, 3)


def _fake(
    symbols: Sequence[tuple[str, str, date, date | None]] | None = None, **tables: pd.DataFrame
) -> FakeSource:
    secs = make_securities(symbols or [("600000", "sh_main", LISTED, None)])
    return FakeSource(trade_days=make_trade_days(DAYS), securities=secs, **tables)


def _bars_calls(source: FakeSource) -> list[tuple[object, ...]]:
    return [args for name, args in source.calls if name == "bars"]


# ---------------------------------------------------------------- update_all


def test_update_resumes_from_last_day(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(store, bars=[make_bars("600000", DAYS[:3], [10.0, 10.1, 10.2])])
    source = _fake(bars=make_bars("600000", DAYS, [10.0 + i / 10 for i in range(10)]))
    report = update_all(source, store, CFG, TODAY)
    assert _bars_calls(source) == [("600000", DAYS[2] + timedelta(days=1), TODAY)]
    assert report.updated == 1
    assert bar_days(store, "600000") == DAYS


def test_first_run_starts_two_years_before_in_sample_start(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    source = _fake(bars=make_bars("600000", DAYS, [10.0] * 10))
    update_all(source, store, CFG, TODAY)
    assert _bars_calls(source)[0][1] == date(2013, 1, 1)  # in_sample_start 2015-01-01 往前 2 年


def test_up_to_date_symbol_counts_as_skipped(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(store, bars=[make_bars("600000", DAYS, [10.0] * 10)])
    source = _fake()
    report = update_all(source, store, CFG, TODAY)
    assert (report.updated, report.skipped, dict(report.failed)) == (0, 1, {})
    assert _bars_calls(source) == []


def test_weekend_today_does_not_refetch_bars(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(store, bars=[make_bars("600000", DAYS, [10.0] * 10)])
    source = _fake()
    report = update_all(source, store, CFG, TODAY + timedelta(days=2))  # 周日
    assert report.skipped == 1
    assert _bars_calls(source) == []


def test_one_symbol_failure_does_not_stop_others(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [("600000", "sh_main", LISTED, None), ("000001", "sz_main", LISTED, None)]
    source = _fake(
        secs,
        bars=pd.concat(
            [make_bars("600000", DAYS, [10.0] * 10), make_bars("000001", DAYS, [5.0] * 10)]
        ),
    )
    source.fail_symbols = {"000001"}
    report = update_all(source, store, CFG, TODAY)
    assert list(report.failed) == ["000001"]
    assert "模拟取数失败" in report.failed["000001"]
    assert report.updated == 1
    assert bar_days(store, "600000") == DAYS
    assert not store.has(Table.BARS, "000001")


def test_symbols_restriction_limits_per_symbol_work_only(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [("600000", "sh_main", LISTED, None), ("000001", "sz_main", LISTED, None)]
    source = _fake(secs, bars=pd.concat([make_bars("600000", DAYS, [10.0] * 10)]))
    report = update_all(source, store, CFG, TODAY, symbols=["600000"])
    assert [c[0] for c in _bars_calls(source)] == ["600000"]
    assert report.updated == 1
    names = {name for name, _ in source.calls}
    assert {"trade_days", "securities", "industries", "st_history", "benchmark"} <= names
    assert len(store.read(Table.SECURITIES)) == 2
    unknown = update_all(_fake(), store, CFG, TODAY, symbols=["999999"])
    assert "999999" in unknown.failed


def test_only_configured_boards_are_stored(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [("600000", "sh_main", LISTED, None), ("300750", "chinext", LISTED, None)]
    source = _fake(secs, bars=make_bars("600000", DAYS, [10.0] * 10))
    update_all(source, store, CFG, TODAY)
    assert store.read(Table.SECURITIES)["symbol"].tolist() == ["600000"]
    assert [c[0] for c in _bars_calls(source)] == ["600000"]


def test_fundamentals_are_refreshed_only_when_missing_or_stale(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    div = make_dividends("600000", [(2023, date(2024, 7, 1), date(2024, 7, 10), 0.3)])
    fin = make_financials("600000", [{"report_period": date(2023, 12, 31), "announce_date": TODAY}])
    populate_store(
        store,
        bars=[make_bars("600000", DAYS, [10.0] * 10)],
        dividends=[div],
        financials=[fin],
    )
    # 滚动刷新：每只股票每 7 天里恰有一天轮到；找出今天轮不到的最近一天作为“不轮到”的日子
    off_day = next(d for d in DAYS if not fundamentals_due("600000", 3, d, 7))
    on_day = next(d for d in DAYS if fundamentals_due("600000", 3, d, 7))

    def fetched(today: date) -> set[str]:
        source = _fake(dividends=div, financials=fin)
        update_all(source, store, CFG, today)
        return {name for name, _ in source.calls if name in ("dividends", "financials")}

    set_file_age(tmp_path, Table.DIVIDENDS, "600000", off_day, 3)
    set_file_age(tmp_path, Table.FINANCIALS, "600000", off_day, 3)
    assert fetched(off_day) == set()  # 文件较新且今天没轮到
    set_file_age(tmp_path, Table.DIVIDENDS, "600000", on_day, 3)
    set_file_age(tmp_path, Table.FINANCIALS, "600000", on_day, 3)
    assert fetched(on_day) == {"dividends", "financials"}  # 轮到的日子才刷新
    set_file_age(tmp_path, Table.DIVIDENDS, "600000", off_day, 14)
    assert fetched(off_day) == {"dividends"}  # 超过两轮必须下载；财务文件刚刷新过
    (tmp_path / "financials" / "600000.parquet").unlink()
    assert "financials" in fetched(off_day)  # 文件缺失


def test_fundamentals_due_spreads_symbols_over_refresh_days() -> None:
    today = date(2026, 11, 16)
    symbols = [f"{i:06d}" for i in range(600000, 600700)]
    due = [s for s in symbols if fundamentals_due(s, age_days=3, today=today, refresh_days=7)]
    assert 60 <= len(due) <= 140  # 约七分之一
    assert fundamentals_due("600000", None, today, 7)  # 文件缺失必须下载
    assert fundamentals_due("600000", 14, today, 7)  # 超过两轮必须下载
    assert not fundamentals_due("600000", 0, today, 7)  # 当天已下载不重复
    week = [today + timedelta(days=i) for i in range(7)]
    assert sum(fundamentals_due("600000", 3, d, 7) for d in week) == 1  # 每轮恰好轮到一次
    assert fundamentals_due("600000", 1, today, 0)  # refresh_days 为 0：每次都下载


def test_refresh_fundamentals_ignores_freshness_and_reports_failures(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    div = pd.concat(
        [
            make_dividends(s, [(2023, date(2024, 7, 1), date(2024, 7, 10), 0.3)])
            for s in ("600001", "600002")
        ]
    )
    fin = pd.concat(
        [
            make_financials(s, [{"report_period": date(2023, 12, 31), "announce_date": TODAY}])
            for s in ("600001", "600002")
        ]
    )
    populate_store(
        store,
        dividends=[div[div["symbol"] == s] for s in ("600001", "600002")],
        financials=[fin[fin["symbol"] == s] for s in ("600001", "600002")],
    )  # 两只的文件都是刚写的
    source = _fake(dividends=div, financials=fin)
    source.fail_symbols = {"600002"}
    failed = refresh_fundamentals(source, store, CFG, ["600001", "600002"])
    assert [a for n, a in source.calls if n == "dividends"] == [("600001",), ("600002",)]
    assert list(failed) == ["600002"]
    assert "模拟取数失败" in failed["600002"]
    assert len(store.read(Table.DIVIDENDS, symbols=["600001"])) == 1


def test_fundamentals_keep_only_rows_from_data_start(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    div = make_dividends(
        "600000",
        [
            (2007, date(2008, 6, 1), date(2008, 6, 10), 0.1),
            (2023, date(2024, 7, 1), date(2024, 7, 10), 0.3),
        ],
    )
    fin = make_financials(
        "600000",
        [
            {"report_period": date(2008, 12, 31), "announce_date": date(2009, 3, 1)},
            {"report_period": date(2009, 12, 31), "announce_date": date(2010, 3, 1)},
        ],
    )
    source = _fake(bars=make_bars("600000", DAYS, [10.0] * 10), dividends=div, financials=fin)
    update_all(source, store, CFG, TODAY)
    assert store.read(Table.DIVIDENDS)["ex_date"].tolist() == [pd.Timestamp("2024-07-10")]
    assert store.read(Table.FINANCIALS)["report_period"].tolist() == [pd.Timestamp("2009-12-31")]


def test_delisted_symbol_without_bars_is_failed_and_countable(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [
        ("600000", "sh_main", LISTED, None),
        ("600001", "sh_main", LISTED, date(2020, 5, 1)),  # 窗口内退市，取不到日线
        ("600002", "sh_main", LISTED, date(2006, 5, 1)),  # 早于下载起点退市：不算失败
    ]
    source = _fake(secs, bars=make_bars("600000", DAYS, [10.0] * 10))
    report = update_all(source, store, CFG, TODAY)
    assert list(report.failed) == ["600001"]
    assert report.failed["600001"].startswith(NO_BARS_PREFIX)
    assert count_no_bars(report.failed) == 1
    assert report.updated == 1
    assert report.skipped == 1
    assert [c[0] for c in _bars_calls(source)] == ["600000", "600001"]


def test_symbol_delisted_between_runs_gets_its_final_bars(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [("600001", "sh_main", LISTED, date(2024, 1, 9))]
    populate_store(store, bars=[make_bars("600001", DAYS[:3], [3.0] * 3)])
    source = _fake(secs, bars=make_bars("600001", DAYS[:8], [3.0] * 8))
    report = update_all(source, store, CFG, TODAY)
    assert _bars_calls(source) == [("600001", DAYS[2] + timedelta(days=1), TODAY)]
    assert report.updated == 1
    assert bar_days(store, "600001") == DAYS[:8]
    # 退市日期 = 最后一根日线的下一个日历日（引擎在其后第一个交易日清仓）
    assert store.read(Table.SECURITIES).iloc[0]["delist_date"] == pd.Timestamp(
        DAYS[7]
    ) + pd.Timedelta(days=1)


def test_long_dead_delisted_symbol_is_not_requested(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    dead = [date(2022, 12, 28), date(2022, 12, 29), date(2022, 12, 30)]
    populate_store(store, bars=[make_bars("600001", dead, [3.0] * 3)])
    source = _fake([("600001", "sh_main", LISTED, date(2023, 1, 3))])
    report = update_all(source, store, CFG, TODAY)
    assert _bars_calls(source) == []
    assert (report.updated, report.skipped) == (0, 1)


def test_recently_dead_delisted_symbol_is_still_requested(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    last = TODAY - timedelta(days=364)
    populate_store(store, bars=[make_bars("600001", [last], [3.0])])
    source = _fake([("600001", "sh_main", LISTED, last)])
    update_all(source, store, CFG, TODAY)
    assert [c[0] for c in _bars_calls(source)] == ["600001"]


def test_bars_after_as_of_are_never_stored(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    as_of = DAYS[5]

    class Leaky(FakeSource):
        def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
            self.calls.append(("bars", (symbol, start, end)))
            return make_bars(symbol, DAYS, [10.0] * 10)  # 无视 end，多给了 as_of 之后的行

    source = Leaky(
        trade_days=make_trade_days(DAYS),
        securities=make_securities([("600000", "sh_main", LISTED, None)]),
    )
    update_all(source, store, CFG, as_of)
    assert _bars_calls(source)[0][2] == as_of
    assert bar_days(store, "600000") == DAYS[:6]


def test_update_all_tells_source_the_as_of_date(tmp_path: Path) -> None:
    source = LookupSource({}, trade_days=make_trade_days(DAYS), securities=make_securities([]))
    update_all(source, ParquetStore(tmp_path), CFG, TODAY)
    assert source.as_of_seen == TODAY


def test_shared_table_failure_raises(tmp_path: Path) -> None:
    class BrokenIndustries(FakeSource):
        def industries(self) -> pd.DataFrame:
            raise ConnectionError("巨潮不可用")

    source = BrokenIndustries(
        trade_days=make_trade_days(DAYS),
        securities=make_securities([("600000", "sh_main", LISTED, None)]),
    )
    with pytest.raises(SharedTableError, match="industries"):
        update_all(source, ParquetStore(tmp_path), CFG, TODAY)


def test_benchmarks_are_filtered_to_trading_days(tmp_path: Path) -> None:
    bench = pd.DataFrame(
        {
            "code": pd.Series(["H00300"] * 3, dtype="str"),
            "day": pd.to_datetime([DAYS[0], date(2024, 1, 6), DAYS[1]]).astype("datetime64[ns]"),
            "close": pd.Series([1.0, 1.0, 2.0], dtype="float64"),  # 1-06 是周六
        }
    )
    store = ParquetStore(tmp_path)
    update_all(_fake(benchmarks=bench), store, CFG, TODAY)
    assert store.read(Table.BENCHMARKS)["day"].tolist() == [
        pd.Timestamp(DAYS[0]),
        pd.Timestamp(DAYS[1]),
    ]


# ---------------------------------------------------------------- check_integrity / load_calendar


CALENDAR = TradingCalendar(weekdays(date(2023, 1, 2), 300))


def _integrity_store(
    tmp_path: Path,
    closes: list[float],
    factors: list[float],
    list_date: date = LISTED,
    days: list[date] | None = None,
) -> tuple[ParquetStore, TradingCalendar]:
    store = ParquetStore(tmp_path)
    use_days = days if days is not None else DAYS[: len(closes)]
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", list_date, None)]),
        bars=[make_bars("600000", use_days, closes, factors)],
    )
    return store, CALENDAR


def _ten_symbols(tmp_path: Path, with_bar: int) -> ParquetStore:
    store = ParquetStore(tmp_path)
    syms = [f"60000{i}" for i in range(10)]
    populate_store(
        store,
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=[make_bars(s, [DAYS[0]], [10.0]) for s in syms[:with_bar]],
    )
    return store


def test_integrity_flags_missing_day(tmp_path: Path) -> None:
    problems = check_integrity(_ten_symbols(tmp_path, 5), CALENDAR, DAYS[0])
    assert len(problems) == 1
    assert "5/10" in problems[0]


def test_integrity_ok_at_exactly_80_percent(tmp_path: Path) -> None:
    assert check_integrity(_ten_symbols(tmp_path, 8), CALENDAR, DAYS[0]) == []


def test_integrity_coverage_can_be_restricted_to_symbols(tmp_path: Path) -> None:
    store = _ten_symbols(tmp_path, 5)  # 600000-600004 有日线
    assert check_integrity(store, CALENDAR, DAYS[0], symbols=["600000", "600001"]) == []
    assert len(check_integrity(store, CALENDAR, DAYS[0], symbols=["600003", "600009"])) == 1


def test_integrity_coverage_ignores_delisted_and_unlisted(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(
        store,
        securities=make_securities(
            [
                ("600000", "sh_main", LISTED, None),
                ("600001", "sh_main", LISTED, DAYS[0]),  # 退市日期当天起不再在市
                ("600002", "sh_main", DAYS[1], None),  # 尚未上市
            ]
        ),
        bars=[make_bars("600000", [DAYS[0]], [10.0])],
    )
    assert check_integrity(store, CALENDAR, DAYS[0]) == []


def test_integrity_skips_non_trading_day(tmp_path: Path) -> None:
    store, calendar = _integrity_store(tmp_path, [10.0], [1.0])
    assert check_integrity(store, calendar, date(2024, 1, 6)) == []  # 周六


def test_integrity_ignores_ex_dividend_gap(tmp_path: Path) -> None:
    store, calendar = _integrity_store(tmp_path, [10.0, 7.0], [1.0, 1.43])
    assert check_integrity(store, calendar, DAYS[1]) == []


def test_integrity_flags_unexplained_jump(tmp_path: Path) -> None:
    store, calendar = _integrity_store(tmp_path, [10.0, 7.0], [1.0, 1.0])
    problems = check_integrity(store, calendar, DAYS[1])
    assert len(problems) == 1
    assert "600000" in problems[0]
    assert str(DAYS[1]) in problems[0]


def test_integrity_only_looks_at_the_given_day(tmp_path: Path) -> None:
    # 历史上的跳变（DAYS[1]）不影响对 DAYS[2] 的检查
    store, calendar = _integrity_store(tmp_path, [10.0, 7.0, 7.1], [1.0, 1.0, 1.0])
    assert check_integrity(store, calendar, DAYS[2]) == []


def test_integrity_skips_jump_after_a_gap(tmp_path: Path) -> None:
    # 停牌后复牌（前一根日线不在前一个交易日）：不查涨跌幅
    store, calendar = _integrity_store(tmp_path, [10.0, 1.0], [1.0, 1.0], days=[DAYS[0], DAYS[2]])
    assert check_integrity(store, calendar, DAYS[2]) == []


def test_integrity_exempts_first_five_trading_days_after_listing(tmp_path: Path) -> None:
    # 上市日 DAYS[0] 是第 1 天；第 2 到 5 天（DAYS[1:5]）免检，第 6 天（DAYS[5]）起检查
    closes = [10.0, 14.0, 5.0, 10.0, 5.0, 10.0]
    store, calendar = _integrity_store(tmp_path, closes, [1.0] * 6, list_date=DAYS[0])
    assert check_integrity(store, calendar, DAYS[4]) == []
    problems = check_integrity(store, calendar, DAYS[5])
    assert len(problems) == 1
    assert str(DAYS[5]) in problems[0]


def test_integrity_jump_check_respects_symbols(tmp_path: Path) -> None:
    store, calendar = _integrity_store(tmp_path, [10.0, 7.0], [1.0, 1.0])
    assert check_integrity(store, calendar, DAYS[1], symbols=["000001"]) == []


def test_integrity_reports_empty_securities(tmp_path: Path) -> None:
    assert check_integrity(ParquetStore(tmp_path), CALENDAR, DAYS[0]) != []


# ---------------------------------------------------------------- latest_closed_day


SH = ZoneInfo("Asia/Shanghai")


def test_latest_closed_day_switches_at_16_00_on_trading_day() -> None:
    friday = date(2024, 1, 5)
    before = datetime(2024, 1, 5, 15, 59, tzinfo=SH)
    after = datetime(2024, 1, 5, 16, 0, tzinfo=SH)
    assert latest_closed_day(before, CALENDAR) == date(2024, 1, 4)
    assert latest_closed_day(after, CALENDAR) == friday


def test_latest_closed_day_on_weekend_is_previous_trading_day() -> None:
    assert latest_closed_day(datetime(2024, 1, 6, 20, 0, tzinfo=SH), CALENDAR) == date(2024, 1, 5)
    assert latest_closed_day(datetime(2024, 1, 7, 9, 0, tzinfo=SH), CALENDAR) == date(2024, 1, 5)


def test_latest_closed_day_converts_other_timezones_and_rejects_naive() -> None:
    utc = datetime(2024, 1, 5, 8, 30, tzinfo=ZoneInfo("UTC"))  # 上海 16:30
    assert latest_closed_day(utc, CALENDAR) == date(2024, 1, 5)
    with pytest.raises(ValueError, match="时区"):
        latest_closed_day(datetime(2024, 1, 5, 17, 0), CALENDAR)


# ---------------------------------------------------------------- st_history 合并


def test_merge_st_history_cases() -> None:
    as_of = date(2026, 11, 12)
    stored = make_st_history(
        [
            ("000010", date(2026, 1, 1), None),  # 仍是 ST：保留原起点
            ("000011", date(2025, 1, 1), date(2025, 6, 1)),  # 已关闭的区间保持不变
            ("000012", date(2026, 2, 1), None),  # 不再是 ST：end 记为 as_of
        ]
    )
    current = make_st_history([("000010", as_of, None), ("000011", as_of, None)])
    merged = merge_st_history(stored, current, as_of)
    rows = {
        (sym, pd.Timestamp(st).date()): (None if pd.isna(en) else pd.Timestamp(en).date())
        for sym, st, en in zip(merged["symbol"], merged["start"], merged["end"], strict=True)
    }
    assert rows == {
        ("000010", date(2026, 1, 1)): None,
        ("000011", date(2025, 1, 1)): date(2025, 6, 1),
        ("000011", as_of): None,  # 摘帽后再次被标记：新增第二个区间
        ("000012", date(2026, 2, 1)): as_of,
    }


def test_merge_st_history_from_empty_store() -> None:
    as_of = date(2026, 11, 12)
    merged = merge_st_history(
        make_st_history([]), make_st_history([("000010", date(2020, 1, 1), None)]), as_of
    )
    assert merged["symbol"].tolist() == ["000010"]
    assert merged["start"].tolist() == [pd.Timestamp(as_of)]  # 起点用 as_of，不用数据源给的日期


def test_update_all_keeps_confirmed_st_days_across_runs(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(store, st_history=make_st_history([("600000", date(2024, 1, 2), None)]))
    source = _fake(st_history=make_st_history([("600000", TODAY, None)]))
    update_all(source, store, CFG, TODAY)
    row = store.read(Table.ST_HISTORY).iloc[0]
    assert row["start"] == pd.Timestamp(2024, 1, 2)
    assert pd.isna(row["end"])


# ---------------------------------------------------------------- 退市日期


def test_symbols_run_still_fixes_delist_dates_of_other_symbols(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [
        ("600000", "sh_main", LISTED, None),
        ("600001", "sh_main", LISTED, date(2024, 1, 3)),  # 沪市“暂停上市日期”早于最后一根日线
    ]
    populate_store(store, bars=[make_bars("600001", DAYS[:6], [3.0] * 6)])
    update_all(
        _fake(secs, bars=make_bars("600000", DAYS, [10.0] * 10)), store, CFG, TODAY, ["600000"]
    )
    row = store.read(Table.SECURITIES).set_index("symbol").loc["600001"]
    assert bool(row["delist_date"] == pd.Timestamp(DAYS[5]) + pd.Timedelta(days=1))


def test_delist_date_missing_is_dropped_and_recorded(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = [
        ("600000", "sh_main", LISTED, None),
        ("600001", "sh_main", LISTED, UNKNOWN_DELIST_DATE.date()),  # 退市但表里没有日期，且没有日线
    ]
    report = update_all(_fake(secs, bars=make_bars("600000", DAYS, [10.0] * 10)), store, CFG, TODAY)
    assert store.read(Table.SECURITIES)["symbol"].tolist() == ["600000"]
    assert report.failed["600001"].startswith(DELIST_MISSING_PREFIX)


# ---------------------------------------------------------------- industries 增量


def _lookup(listed: list[str], classes: dict[str, tuple[str, bool] | None]) -> LookupSource:
    return LookupSource(
        classes,
        trade_days=make_trade_days(DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in listed]),
    )


def test_industries_are_queried_incrementally(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    classes: dict[str, tuple[str, bool] | None] = {
        "600000": ("货币金融服务", True),
        "600001": ("制造业", False),
        "600002": ("房地产业", False),
    }
    first = _lookup(["600000", "600001"], classes)
    update_all(first, store, CFG, TODAY)
    assert first.industry_calls == ["600000", "600001"]
    assert store.read(Table.INDUSTRIES)["is_financial"].tolist() == [True, False]

    second = _lookup(["600000", "600001"], classes)
    update_all(second, store, CFG, TODAY)
    assert second.industry_calls == []  # 文件还新，且没有缺失的股票

    third = _lookup(["600000", "600001", "600002"], classes)
    update_all(third, store, CFG, TODAY)
    assert third.industry_calls == ["600002"]
    assert store.read(Table.INDUSTRIES)["symbol"].tolist() == ["600000", "600001", "600002"]


def test_industries_fully_requeried_when_file_is_stale(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    classes: dict[str, tuple[str, bool] | None] = {
        "600000": ("银行", True),
        "600001": ("制造业", False),
    }
    update_all(_lookup(["600000", "600001"], classes), store, CFG, TODAY)
    set_table_age(tmp_path, Table.INDUSTRIES, TODAY, 31)
    again = _lookup(["600000", "600001"], classes)
    update_all(again, store, CFG, TODAY)
    assert again.industry_calls == ["600000", "600001"]


def test_industry_failure_is_per_symbol_and_does_not_abort(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    source = _lookup(["600000", "600001"], {"600000": ("银行", True), "600001": ("制造业", False)})
    source.industry_fail = {"600001"}
    report = update_all(source, store, CFG, TODAY)
    assert report.failed["600001"].startswith(INDUSTRY_FAILED_PREFIX)
    assert store.read(Table.INDUSTRIES)["symbol"].tolist() == ["600000"]


def test_industry_coverage_below_threshold_is_a_problem(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    listed = [f"60000{i}" for i in range(10)]
    classes: dict[str, tuple[str, bool] | None] = {s: ("制造业", False) for s in listed[:8]}
    report = update_all(_lookup(listed, classes), store, CFG, TODAY)
    assert any("行业" in p and "8/10" in p for p in report.problems)  # 80% < 90%
    ok = update_all(_lookup(listed, {s: ("制造业", False) for s in listed[:9]}), store, CFG, TODAY)
    assert ok.problems == []  # 补上第 9 只后覆盖 90%


def test_industries_fallback_to_full_table_for_sources_without_lookup(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    source = _fake(industries=make_industries([("600000", "银行", True)]))
    report = update_all(source, store, CFG, TODAY)
    assert store.read(Table.INDUSTRIES)["symbol"].tolist() == ["600000"]
    assert report.problems == []


def test_progress_reports_stage_and_interval(tmp_path: Path) -> None:
    seen: list[tuple[str, int, int]] = []
    listed = [f"60000{i}" for i in range(5)]
    source = _lookup(listed, {s: ("制造业", False) for s in listed})

    def record(stage: str, done: int, total: int) -> None:
        seen.append((stage, done, total))

    update_all(source, ParquetStore(tmp_path), CFG, TODAY, progress=record, progress_every=2)
    assert ("行业", 2, 5) in seen and ("行业", 4, 5) in seen
    assert ("日线", 2, 5) in seen and ("日线", 4, 5) in seen


def test_load_calendar(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(DataIntegrityError, match="trade_days"):
        load_calendar(store)
    store.write(Table.TRADE_DAYS, make_trade_days(DAYS))
    calendar = load_calendar(store)
    assert calendar.is_trading_day(DAYS[0])
    assert not calendar.is_trading_day(date(2024, 1, 6))
