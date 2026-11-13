"""update_all 的中断与部分运行：磁盘上的表任何时候都是定稿状态。"""

import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import qt.data.update as update_module
from qt.config import AppConfig
from qt.data.delist import UNKNOWN_DELIST_DATE
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.data.update import NetworkOutageError, SharedTableError, merge_st_history, update_all
from tests.data.helpers import (
    BrokenBenchmarkSource,
    InterruptingSource,
    LookupSource,
    make_trade_days,
    set_table_age,
)
from tests.fakes import (
    FakeSource,
    default_app_config,
    make_bars,
    make_industries,
    make_securities,
    make_st_history,
    populate_store,
    weekdays,
)

CFG = default_app_config()
DAYS = weekdays(date(2024, 1, 1), 10)
AS_OF = DAYS[-1]
LISTED = date(2000, 1, 3)
PLACEHOLDER = UNKNOWN_DELIST_DATE.date()


def _no_placeholder(store: ParquetStore) -> None:
    secs = store.read(Table.SECURITIES)
    assert (secs["delist_date"].dropna().dt.year < 2200).all()


def test_failed_benchmark_still_leaves_finalized_securities(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(
        store,
        bars=[
            make_bars("600005", DAYS[:6], [3.0] * 6),
            make_bars("600010", DAYS[:4], [3.0] * 4),
        ],
    )
    secs = make_securities(
        [
            ("600005", "sh_main", LISTED, DAYS[1]),  # 暂停上市日期早于最后日线
            ("600009", "sh_main", LISTED, PLACEHOLDER),  # 无日期无日线：丢弃
            ("600010", "sh_main", LISTED, PLACEHOLDER),  # 无日期但有日线
        ]
    )
    source = BrokenBenchmarkSource(trade_days=make_trade_days(DAYS), securities=secs)
    with pytest.raises(SharedTableError):
        update_all(source, store, CFG, AS_OF)
    stored = store.read(Table.SECURITIES).set_index("symbol")
    assert sorted(stored.index) == ["600005", "600010"]
    assert bool(stored.loc["600005", "delist_date"] == pd.Timestamp(DAYS[5]) + pd.Timedelta(days=1))
    assert bool(stored.loc["600010", "delist_date"] == pd.Timestamp(DAYS[3]) + pd.Timedelta(days=1))
    _no_placeholder(store)


def test_interrupt_in_symbol_loop_finalizes_and_propagates(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = make_securities(
        [
            ("600001", "sh_main", LISTED, DAYS[0]),  # 已退市，本次运行补到 DAYS[:4] 的日线
            ("600002", "sh_main", LISTED, None),  # 第 2 次 bars() 调用时被中断
        ]
    )
    source = InterruptingSource(
        2,
        trade_days=make_trade_days(DAYS),
        securities=secs,
        bars=pd.concat([make_bars("600001", DAYS[:4], [3.0] * 4)]),
    )
    with pytest.raises(KeyboardInterrupt):
        update_all(source, store, CFG, AS_OF)
    row = store.read(Table.SECURITIES).set_index("symbol").loc["600001"]
    assert bool(row["delist_date"] == pd.Timestamp(DAYS[3]) + pd.Timedelta(days=1))
    _no_placeholder(store)


def _placeholder_source(
    with_bars: bool, listed_bars: bool = True, source_cls: type[FakeSource] = FakeSource
) -> FakeSource:
    secs = make_securities(
        [("600000", "sh_main", LISTED, None), ("600009", "sh_main", LISTED, PLACEHOLDER)]
    )
    frames: list[pd.DataFrame] = []
    if listed_bars:
        frames.append(make_bars("600000", DAYS, [10.0] * 10))
    if with_bars:
        frames.append(make_bars("600009", DAYS[:4], [3.0] * 4))
    tables: dict[str, pd.DataFrame] = {}
    if frames:
        tables["bars"] = pd.concat(frames)
    return source_cls(trade_days=make_trade_days(DAYS), securities=secs, **tables)


def _bars_requested(source: FakeSource) -> list[str]:
    return [str(args[0]) for name, args in source.calls if name == "bars"]


def test_placeholder_symbol_with_source_bars_is_downloaded_and_finalized(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    for _ in range(2):  # 第二次运行结果相同
        report = update_all(_placeholder_source(with_bars=True), store, CFG, AS_OF)
        assert "600009" not in report.failed
        assert store.last_day(Table.BARS, "600009") == DAYS[3]
        row = store.read(Table.SECURITIES).set_index("symbol").loc["600009"]
        assert bool(row["delist_date"] == pd.Timestamp(DAYS[3]) + pd.Timedelta(days=1))
        _no_placeholder(store)


def test_placeholder_symbol_without_bars_is_dropped_once_and_retried_every_run(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    for _ in range(2):
        source = _placeholder_source(with_bars=False)
        report = update_all(source, store, CFG, AS_OF)
        assert "600009" in _bars_requested(source)  # 每次运行都重新请求
        assert store.read(Table.SECURITIES)["symbol"].tolist() == ["600000"]
        message = report.failed["600009"]
        assert message.count("退市日期缺失") == 1
        assert "无日线数据" in message  # 两个事实都保留
        _no_placeholder(store)


def test_interrupt_finalizes_reached_placeholder_symbols_only(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = make_securities(
        [
            ("600001", "sh_main", LISTED, PLACEHOLDER),  # 中断前已补到日线
            ("600002", "sh_main", LISTED, PLACEHOLDER),  # 第 2 次 bars() 调用被中断，没到
        ]
    )
    source = InterruptingSource(
        2,
        trade_days=make_trade_days(DAYS),
        securities=secs,
        bars=pd.concat([make_bars("600001", DAYS[:4], [3.0] * 4)]),
    )
    with pytest.raises(KeyboardInterrupt):
        update_all(source, store, CFG, AS_OF)
    stored = store.read(Table.SECURITIES).set_index("symbol")
    assert stored.index.tolist() == ["600001"]
    assert bool(stored.loc["600001", "delist_date"] == pd.Timestamp(DAYS[3]) + pd.Timedelta(days=1))
    _no_placeholder(store)


def test_symbols_run_keeps_previously_finalized_placeholder_symbol(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    update_all(_placeholder_source(with_bars=True), store, CFG, AS_OF)
    source = _placeholder_source(with_bars=True)
    report = update_all(source, store, CFG, AS_OF, symbols=["600000"])
    assert "600009" not in _bars_requested(source)  # 没被尝试
    assert "600009" not in report.failed
    assert store.read(Table.SECURITIES)["symbol"].tolist() == ["600000", "600009"]
    _no_placeholder(store)


def test_symbols_run_does_not_report_unattempted_dropped_symbol(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    report = update_all(_placeholder_source(with_bars=False), store, CFG, AS_OF, symbols=["600000"])
    assert "600009" not in report.failed
    assert store.read(Table.SECURITIES)["symbol"].tolist() == ["600000"]


def test_benchmarks_after_as_of_are_dropped(tmp_path: Path) -> None:
    bench = pd.DataFrame(
        {
            "code": pd.Series(["H00300"] * 4, dtype="str"),
            "day": pd.to_datetime(DAYS[3:7]).astype("datetime64[ns]"),
            "close": pd.Series([1.0, 2.0, 3.0, 4.0], dtype="float64"),
        }
    )

    class LeakyBenchmark(FakeSource):
        def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame:
            return bench[bench["code"] == code].reset_index(drop=True)  # 无视 end

    source = LeakyBenchmark(trade_days=make_trade_days(DAYS), securities=make_securities([]))
    store = ParquetStore(tmp_path)
    update_all(source, store, CFG, DAYS[5])
    assert store.read(Table.BENCHMARKS)["day"].max() == pd.Timestamp(DAYS[5])


def test_empty_st_list_with_open_intervals_is_an_error(tmp_path: Path) -> None:
    open_rows = make_st_history([("000010", date(2026, 1, 1), None)])
    with pytest.raises(ValueError, match="ST"):
        merge_st_history(open_rows, make_st_history([]), date(2026, 11, 12))
    closed = make_st_history([("000010", date(2025, 1, 1), date(2025, 6, 1))])
    assert len(merge_st_history(closed, make_st_history([]), date(2026, 11, 12))) == 1

    store = ParquetStore(tmp_path)
    populate_store(store, st_history=open_rows)
    source = LookupSource({}, trade_days=make_trade_days(DAYS), securities=make_securities([]))
    with pytest.raises(SharedTableError, match="st_history"):
        update_all(source, store, CFG, AS_OF)
    assert store.read(Table.ST_HISTORY)["end"].isna().all()  # 原有区间没有被关闭


def test_symbols_run_neither_refreshes_stale_industries_nor_resets_the_clock(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    classes: dict[str, tuple[str, bool] | None] = {
        s: ("制造业", False) for s in ("600000", "600001", "600002")
    }
    listed = make_securities([(s, "sh_main", LISTED, None) for s in classes])
    update_all(
        LookupSource(classes, trade_days=make_trade_days(DAYS), securities=listed),
        store,
        CFG,
        AS_OF,
    )
    set_table_age(tmp_path, Table.INDUSTRIES, AS_OF, 40)
    # 删掉 600002 的行，制造“缺失”
    inds = store.read(Table.INDUSTRIES)
    store.write(Table.INDUSTRIES, inds[inds["symbol"] != "600002"])
    set_table_age(tmp_path, Table.INDUSTRIES, AS_OF, 40)

    partial = LookupSource(classes, trade_days=make_trade_days(DAYS), securities=listed)
    update_all(partial, store, CFG, AS_OF, symbols=["600000", "600002"])
    assert partial.industry_calls == ["600002"]  # 过期了也不全量重查，只补缺失的
    stamp = store.modified_time(Table.INDUSTRIES)
    assert stamp is not None
    assert (AS_OF - stamp.date()).days == 40  # 部分运行不重置过期时钟

    full = LookupSource(classes, trade_days=make_trade_days(DAYS), securities=listed)
    update_all(full, store, CFG, AS_OF)
    assert full.industry_calls == ["600000", "600001", "600002"]  # 全量运行才处理过期


# ---------------------------------------------------------------- securities 不缩水（I2）


def _listed_source(symbols: list[str], **tables: pd.DataFrame) -> FakeSource:
    secs = make_securities([(s, "sh_main", LISTED, None) for s in symbols])
    return FakeSource(
        trade_days=make_trade_days(DAYS),
        securities=secs,
        industries=make_industries([(s, "制造业", False) for s in symbols]),
        **tables,
    )


def _shrink_problems(problems: Sequence[str]) -> list[str]:
    return [p for p in problems if "证券" in p]


def test_first_run_has_no_shrink_problem(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    symbols = [f"600{i:03d}" for i in range(5)]
    report = update_all(_listed_source(symbols), store, CFG, AS_OF, symbols=symbols[:1])
    assert _shrink_problems(report.problems) == []
    assert store.read(Table.SECURITIES)["symbol"].tolist() == symbols


def test_symbol_absent_from_later_source_is_kept(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    symbols = [f"600{i:03d}" for i in range(100)]
    update_all(_listed_source(symbols), store, CFG, AS_OF, symbols=symbols[1:2])
    before = store.read(Table.SECURITIES)
    report = update_all(_listed_source(symbols[1:]), store, CFG, AS_OF, symbols=symbols[1:2])
    after = store.read(Table.SECURITIES)
    assert after["symbol"].tolist() == symbols  # 600000 不在本次数据源里，仍保留上次的行
    pd.testing.assert_frame_equal(after, before)
    assert _shrink_problems(report.problems) == []  # 少 1%，未超过 2%


def test_empty_delisted_list_keeps_stored_delisted_rows_and_reports_problem(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    populate_store(store, bars=[make_bars("600005", DAYS[:6], [3.0] * 6)])
    rows = [
        ("600000", "sh_main", LISTED, None),
        ("600001", "sh_main", LISTED, None),
        ("600005", "sh_main", LISTED, DAYS[1]),  # 有日线：退市日定稿为最后日线 + 1 天
        ("600006", "sh_main", LISTED, date(2023, 5, 1)),  # 无日线：沿用退市表日期
    ]
    first = FakeSource(trade_days=make_trade_days(DAYS), securities=make_securities(rows))
    update_all(first, store, CFG, AS_OF, symbols=["600000"])
    before = store.read(Table.SECURITIES)
    second = FakeSource(trade_days=make_trade_days(DAYS), securities=make_securities(rows[:2]))
    report = update_all(second, store, CFG, AS_OF, symbols=["600000"])
    pd.testing.assert_frame_equal(store.read(Table.SECURITIES), before)
    (problem,) = _shrink_problems(report.problems)
    assert "2 只" in problem and "4 只" in problem


# ---------------------------------------------------------------- 断网熔断（I7）


class _OutageSource(FakeSource):
    """bars() 对 down 中的股票抛网络错误。"""

    def __init__(self, down: set[str], **tables: pd.DataFrame) -> None:
        super().__init__(**tables)
        self.down = down

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol in self.down:
            self.calls.append(("bars", (symbol, start, end)))
            raise ConnectionError(f"模拟断网：{symbol}")
        return super().bars(symbol, start, end)


def _limit(n: int) -> AppConfig:
    return CFG.model_copy(
        update={"data": CFG.data.model_copy(update={"max_consecutive_failures": n})}
    )


def test_consecutive_network_failures_abort_the_run(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    symbols = [f"60000{i}" for i in range(6)]
    secs = make_securities(
        [("600009", "sh_main", LISTED, DAYS[0])]  # 已退市，本次补到日线
        + [(s, "sh_main", LISTED, None) for s in symbols]
    )
    source = _OutageSource(
        set(symbols),
        trade_days=make_trade_days(DAYS),
        securities=secs,
        bars=make_bars("600009", DAYS[:4], [3.0] * 4),
    )
    with pytest.raises(NetworkOutageError, match="连续 3 只"):
        update_all(source, store, _limit(3), AS_OF)
    requested = [str(args[0]) for name, args in source.calls if name == "bars"]
    assert requested == ["600009", "600000", "600001", "600002"]
    # 熔断后仍然定稿了 securities
    row = store.read(Table.SECURITIES).set_index("symbol").loc["600009"]
    assert bool(row["delist_date"] == pd.Timestamp(DAYS[3]) + pd.Timedelta(days=1))


def test_success_resets_the_failure_streak_and_other_errors_do_not_count(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    symbols = [f"60000{i}" for i in range(7)]
    source = _OutageSource(
        {"600000", "600001", "600003", "600004"},
        trade_days=make_trade_days(DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in symbols]),
        bars=pd.concat([make_bars(s, DAYS, [10.0] * 10) for s in ("600002", "600006")]),
    )
    source.fail_symbols = {"600005"}  # RuntimeError：不是网络错误，不计入连续失败
    report = update_all(source, store, _limit(3), AS_OF)
    assert sorted(report.failed) == ["600000", "600001", "600003", "600004", "600005"]


def test_delisted_symbols_without_bars_do_not_trip_the_breaker(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    secs = make_securities([(f"60000{i}", "sh_main", LISTED, DAYS[0]) for i in range(5)])
    source = FakeSource(trade_days=make_trade_days(DAYS), securities=secs)
    report = update_all(source, store, _limit(2), AS_OF)
    assert len(report.failed) == 5
    assert all("无日线数据" in m for m in report.failed.values())


# ---------------------------------------------------------------- 定稿失败不掩盖原异常


def test_finalize_error_does_not_mask_the_original_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []
    real = update_module.finalize_securities

    def flaky(secs: pd.DataFrame, store: ParquetStore) -> tuple[pd.DataFrame, list[str]]:
        calls.append(1)
        if len(calls) >= 2:
            raise OSError("模拟定稿写盘失败")
        return real(secs, store)

    monkeypatch.setattr(update_module, "finalize_securities", flaky)
    source = InterruptingSource(
        1,
        trade_days=make_trade_days(DAYS),
        securities=make_securities([("600000", "sh_main", LISTED, None)]),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(KeyboardInterrupt):
        update_all(source, ParquetStore(tmp_path), CFG, AS_OF)
    assert "模拟定稿写盘失败" in caplog.text
