"""增量更新、完整性检查与命令行测试（不联网）。"""

import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

import qt.cli
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    NO_BARS_PREFIX,
    DataIntegrityError,
    SharedTableError,
    check_integrity,
    count_no_bars,
    load_calendar,
    update_all,
)
from tests.data.helpers import bar_days, make_trade_days, set_file_age
from tests.fakes import (
    FakeSource,
    default_app_config,
    make_bars,
    make_dividends,
    make_financials,
    make_securities,
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

    def fetched() -> set[str]:
        source = _fake(dividends=div, financials=fin)
        update_all(source, store, CFG, TODAY)
        return {name for name, _ in source.calls if name in ("dividends", "financials")}

    set_file_age(tmp_path, Table.DIVIDENDS, "600000", TODAY, 3)
    set_file_age(tmp_path, Table.FINANCIALS, "600000", TODAY, 3)
    assert fetched() == set()  # 都不足 7 天
    set_file_age(tmp_path, Table.DIVIDENDS, "600000", TODAY, 7)
    assert fetched() == set()  # 恰好 7 天不算“超过”
    set_file_age(tmp_path, Table.DIVIDENDS, "600000", TODAY, 8)
    assert fetched() == {"dividends"}
    set_file_age(tmp_path, Table.FINANCIALS, "600000", TODAY, 30)
    assert fetched() == {"financials"}  # 分红在上一次刷新后已是新文件，只剩财务过期
    (tmp_path / "dividends" / "600000.parquet").unlink()
    assert "dividends" in fetched()  # 文件缺失


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


def test_delisted_symbol_with_stored_bars_is_not_refetched_and_delist_date_is_capped(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    delist = date(2024, 1, 10)
    secs = [("600001", "sh_main", LISTED, delist)]
    populate_store(store, bars=[make_bars("600001", DAYS[:5], [3.0] * 5)])  # 最后一根 1-05
    source = _fake(secs)
    report = update_all(source, store, CFG, TODAY)
    assert _bars_calls(source) == []
    assert report.skipped == 1
    row = store.read(Table.SECURITIES).iloc[0]
    assert row["delist_date"] == pd.Timestamp(DAYS[4])  # min(退市表日期, 最后一根日线)


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


def _integrity_store(
    tmp_path: Path, closes: list[float], factors: list[float], list_date: date = LISTED
) -> tuple[ParquetStore, TradingCalendar]:
    store = ParquetStore(tmp_path)
    days = DAYS[: len(closes)]
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", list_date, None)]),
        bars=[make_bars("600000", days, closes, factors)],
    )
    return store, TradingCalendar(weekdays(date(2023, 1, 2), 300))


def test_integrity_flags_missing_day(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    syms = [f"60000{i}" for i in range(10)]
    populate_store(
        store,
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=[make_bars(s, [DAYS[0]], [10.0]) for s in syms[:5]],
    )
    problems = check_integrity(store, TradingCalendar(DAYS), DAYS[0])
    assert len(problems) == 1
    assert "5/10" in problems[0]


def test_integrity_ok_at_exactly_80_percent(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    syms = [f"60000{i}" for i in range(5)]
    populate_store(
        store,
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=[make_bars(s, [DAYS[0]], [10.0]) for s in syms[:4]],
    )
    assert check_integrity(store, TradingCalendar(DAYS), DAYS[0]) == []


def test_integrity_skips_coverage_check_on_non_trading_day(tmp_path: Path) -> None:
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


def test_integrity_exempts_first_five_trading_days_after_listing(tmp_path: Path) -> None:
    # 上市日 DAYS[0] 是第 1 天；第 2 到 5 天（DAYS[1:5]）免检，第 6 天（DAYS[5]）起检查
    closes = [10.0, 14.0, 5.0, 10.0, 5.0, 10.0]
    store, calendar = _integrity_store(tmp_path, closes, [1.0] * 6, list_date=DAYS[0])
    problems = check_integrity(store, calendar, DAYS[5])
    assert len(problems) == 1
    assert str(DAYS[5]) in problems[0]


def test_integrity_reports_empty_securities(tmp_path: Path) -> None:
    assert check_integrity(ParquetStore(tmp_path), TradingCalendar(DAYS), DAYS[0]) != []


def test_load_calendar(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    with pytest.raises(DataIntegrityError, match="trade_days"):
        load_calendar(store)
    store.write(Table.TRADE_DAYS, make_trade_days(DAYS))
    calendar = load_calendar(store)
    assert calendar.is_trading_day(DAYS[0])
    assert not calendar.is_trading_day(date(2024, 1, 6))


# ---------------------------------------------------------------- 命令行


def _cli_source(bad: set[str] | None = None, n_symbols: int = 5) -> FakeSource:
    today = date.today()
    days = weekdays(today - timedelta(days=40), 40)
    days = [d for d in days if d <= today]
    syms = [f"60000{i}" for i in range(n_symbols)]
    source = FakeSource(
        trade_days=make_trade_days(days),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=pd.concat([make_bars(s, days, [10.0] * len(days)) for s in syms]),
    )
    source.fail_symbols = bad or set()
    return source


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, source: DataSource, data_dir: Path, *extra: str
) -> tuple[int, str]:

    def fake_make_source(_cfg: AppConfig) -> DataSource:
        return source

    monkeypatch.setattr(qt.cli, "make_source", fake_make_source)
    result = CliRunner().invoke(qt.cli.app, ["data", "update", "--data-dir", str(data_dir), *extra])
    return result.exit_code, result.output


def test_cli_failed_symbols_alone_do_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, output = _run_cli(monkeypatch, _cli_source({"600004"}), tmp_path)
    assert code == 0, output
    assert "失败 1 只" in output
    assert "完整性检查通过" in output
    failures = json.loads((tmp_path / "update-failures.json").read_text(encoding="utf-8"))
    assert list(failures) == ["600004"]


def test_cli_integrity_problem_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, output = _run_cli(monkeypatch, _cli_source({"600003", "600004"}), tmp_path)
    assert code == 1
    assert "3/5" in output


def test_cli_shared_table_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Broken(FakeSource):
        def st_history(self) -> pd.DataFrame:
            raise ConnectionError("断线")

    source = Broken(trade_days=make_trade_days(weekdays(date(2024, 1, 1), 5)))
    code, output = _run_cli(monkeypatch, source, tmp_path)
    assert code == 1
    assert "st_history" in output


def test_cli_symbols_option_and_progress(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = _cli_source(n_symbols=5)
    # 只更新 2/5 只股票，覆盖率不足，完整性检查会让退出码非零：这里只关心更新了哪些股票
    code, output = _run_cli(monkeypatch, source, tmp_path, "--symbols", "600000,600001")
    assert code == 1
    assert "2/5" in output
    assert sorted({str(c[1][0]) for c in source.calls if c[0] == "bars"}) == ["600000", "600001"]
