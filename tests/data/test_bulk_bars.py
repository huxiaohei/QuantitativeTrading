"""每晚日线改用批量行情：整体闸门、逐只判断、停牌、回退逐只请求（不联网）。"""

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import SNAPSHOT_SCHEMA, Table
from qt.data.store import ParquetStore
from qt.data.update import (
    SNAPSHOT_MAX_NO_TRADE,
    SNAPSHOT_MIN_COVERAGE,
    SNAPSHOT_MIN_MATCH,
    SNAPSHOT_TOLERANCE,
    NetworkOutageError,
    UpdateReport,
    format_bulk,
    snapshot_window_problem,
    update_all,
)
from tests.data.helpers import bar_days, make_trade_days, set_file_age
from tests.fakes import (
    FakeSource,
    default_app_config,
    make_dividends,
    make_financials,
    make_industries,
    make_securities,
    populate_store,
    snapshot_from_bars,
    weekdays,
)

CFG: AppConfig = default_app_config()
SH = ZoneInfo("Asia/Shanghai")
DAYS = weekdays(date(2024, 1, 1), 10)  # 2024-01-01 至 2024-01-12（周五）
NEXT = date(2024, 1, 15)  # 下一个交易日（周一）
CAL_DAYS = [*DAYS, NEXT]
AS_OF = DAYS[-1]
PREV = DAYS[-2]
NOW = datetime(2024, 1, 12, 20, 0, tzinfo=SH)  # as_of 收盘之后
LISTED = date(2000, 1, 3)


def _symbols(n: int) -> list[str]:
    return [f"6000{i:02d}" for i in range(n)]


def _bars(symbol: str, base: float) -> pd.DataFrame:
    """开高低收各不相同、成交量与成交额不规整、复权因子中途变化的日线（as_of 当天不除权）。"""
    n = len(DAYS)
    close = [round(base + 0.01 * i * (1 if i % 2 else -1), 2) for i in range(n)]
    factors = [1.0] * 4 + [1.2345678901234] * (n - 4)
    return pd.DataFrame(
        {
            "symbol": pd.Series([symbol] * n, dtype="str"),
            "day": pd.to_datetime(DAYS).astype("datetime64[ns]"),
            "open": pd.Series([round(c - 0.03, 2) for c in close], dtype="float64"),
            "high": pd.Series([round(c + 0.07, 2) for c in close], dtype="float64"),
            "low": pd.Series([round(c - 0.11, 2) for c in close], dtype="float64"),
            "close": pd.Series(close, dtype="float64"),
            "volume": pd.Series([123_456.0 + 7 * i for i in range(n)], dtype="float64"),
            "amount": pd.Series([1_234_567.89 + 13.1 * i for i in range(n)], dtype="float64"),
            "adj_factor": pd.Series(factors, dtype="float64"),
        }
    )


def _market(n: int) -> pd.DataFrame:
    return pd.concat([_bars(s, 10.0 + i) for i, s in enumerate(_symbols(n))], ignore_index=True)


def _source(market: pd.DataFrame, snapshot: pd.DataFrame | None) -> FakeSource:
    syms = sorted(set(market["symbol"]))
    source = FakeSource(
        trade_days=make_trade_days(CAL_DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=market,
        industries=make_industries([(s, "制造业", False) for s in syms]),
    )
    source.snapshot = snapshot
    return source


def _store(tmp_path: Path, market: pd.DataFrame, through: date = PREV) -> ParquetStore:
    """库里的日线到 through 为止。"""
    store = ParquetStore(tmp_path)
    kept = market[market["day"] <= pd.Timestamp(through)]
    populate_store(store, bars=[g.reset_index(drop=True) for _, g in kept.groupby("symbol")])
    return store


def _snapshot(market: pd.DataFrame) -> pd.DataFrame:
    return snapshot_from_bars(market, AS_OF, PREV)


def _set(snapshot: pd.DataFrame, symbol: str, **values: float) -> None:
    for col, value in values.items():
        snapshot.loc[snapshot["symbol"] == symbol, col] = value


def _bars_symbols(source: FakeSource) -> list[str]:
    return [str(args[0]) for name, args in source.calls if name == "bars"]


def _snapshot_calls(source: FakeSource) -> int:
    return sum(1 for name, _ in source.calls if name == "daily_snapshot")


def _run(source: FakeSource, store: ParquetStore, now: datetime | None = NOW) -> UpdateReport:
    return update_all(source, store, CFG, AS_OF, now=now)


# ---------------------------------------------------------------- 正常使用


def test_bulk_writes_as_of_bars_without_per_symbol_requests(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert _snapshot_calls(source) == 1
    assert _bars_symbols(source) == []
    assert (report.bulk_used, report.bulk_bars, report.per_symbol_bars) == (True, 3, 0)
    assert report.bulk_reason == ""
    assert (report.updated, report.skipped, dict(report.failed)) == (3, 0, {})
    for symbol in _symbols(3):
        assert bar_days(store, symbol) == DAYS
    # 分红与财务照常逐只处理
    assert {str(a[0]) for n, a in source.calls if n == "dividends"} == set(_symbols(3))


def test_bulk_bar_is_identical_to_per_symbol_bar(tmp_path: Path) -> None:
    market = _market(3)
    bulk_store = _store(tmp_path / "bulk", market)
    single_store = _store(tmp_path / "single", market)
    bulk = _run(_source(market, _snapshot(market)), bulk_store)
    single = _run(_source(market, None), single_store)
    assert (bulk.bulk_used, single.bulk_used) == (True, False)
    assert single.per_symbol_bars == 3
    left = bulk_store.read(Table.BARS)
    right = single_store.read(Table.BARS)
    assert list(left.columns) == list(right.columns)
    assert list(left.dtypes) == list(right.dtypes)
    pd.testing.assert_frame_equal(left, right, check_exact=True)
    for symbol in _symbols(3):
        name = f"{symbol}.parquet"
        assert (tmp_path / "bulk" / "bars" / name).read_bytes() == (
            tmp_path / "single" / "bars" / name
        ).read_bytes()


def test_bulk_bar_carries_previous_adj_factor_exactly(tmp_path: Path) -> None:
    market = _market(1)
    store = _store(tmp_path, market)
    _run(_source(market, _snapshot(market)), store)
    bars = store.read(Table.BARS, symbols=["600000"])
    assert bars["adj_factor"].iloc[-1] == 1.2345678901234
    assert bars["day"].iloc[-1] == pd.Timestamp(AS_OF)


# ---------------------------------------------------------------- 逐只判断


def test_prev_close_mismatch_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600001", prev_close=9.0)  # 当天除权：昨收参考价被调整
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600001"]
    assert (report.bulk_bars, report.per_symbol_bars) == (9, 1)
    assert bar_days(store, "600001") == DAYS


def test_prev_close_tolerance_boundary(tmp_path: Path) -> None:
    market = _market(20)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    stored = {
        s: float(c)
        for s, c, d in zip(market["symbol"], market["close"], market["day"], strict=True)
        if d == pd.Timestamp(PREV)
    }
    # 价格只有两位小数，0.005 的容差只用来吸收浮点误差；正好 0.005 的差值在浮点里不可靠，故测两侧
    _set(snapshot, "600000", prev_close=stored["600000"] + SNAPSHOT_TOLERANCE - 0.001)
    _set(snapshot, "600002", prev_close=stored["600002"] - SNAPSHOT_TOLERANCE + 0.001)
    _set(snapshot, "600001", prev_close=stored["600001"] + SNAPSHOT_TOLERANCE + 0.001)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600001"]


@pytest.mark.parametrize(
    "values",
    [{"open": 0.0}, {"close": 0.0}, {"volume": float("nan")}, {"close": float("nan")}],
)
def test_untrusted_quote_goes_per_symbol(tmp_path: Path, values: dict[str, float]) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600002", **values)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600002"]
    assert bar_days(store, "600002") == DAYS


def test_symbol_missing_from_snapshot_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    snapshot = snapshot[snapshot["symbol"] != "600001"].reset_index(drop=True)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600001"]


def test_lagging_or_never_downloaded_symbols_go_per_symbol(tmp_path: Path) -> None:
    market = _market(4)
    store = _store(tmp_path, market)
    lagging = market[market["symbol"] == "600002"]
    store.write(
        Table.BARS,
        lagging[lagging["day"] <= pd.Timestamp(DAYS[-3])].reset_index(drop=True),
        symbol="600002",
    )
    (tmp_path / "bars" / "600003.parquet").unlink()
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert report.bulk_used
    assert sorted(_bars_symbols(source)) == ["600002", "600003"]
    assert (report.bulk_bars, report.per_symbol_bars) == (2, 2)
    assert bar_days(store, "600002") == DAYS
    assert bar_days(store, "600003") == DAYS


def test_suspended_symbol_gets_no_bar_and_no_request(tmp_path: Path) -> None:
    market = _market(10)  # 1/10 无成交，不超过整晚闸门的 10%
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600001", open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0, amount=0.0)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == []
    assert (report.bulk_bars, report.bulk_suspended, report.per_symbol_bars) == (9, 1, 0)
    assert (report.updated, report.skipped, dict(report.failed)) == (9, 1, {})
    assert bar_days(store, "600001") == DAYS[:-1]


def test_suspended_symbol_lagging_more_than_a_day_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    own = market[market["symbol"] == "600001"]
    store.write(
        Table.BARS, own[own["day"] <= pd.Timestamp(DAYS[-4])].reset_index(drop=True), "600001"
    )
    snapshot = _snapshot(market)
    _set(snapshot, "600001", open=0.0, close=0.0, volume=0.0, amount=0.0)
    source = _source(market, snapshot)
    _run(source, store)
    assert _bars_symbols(source) == ["600001"]
    assert bar_days(store, "600001") == DAYS  # 中间缺的日子补上了


def test_duplicated_code_in_snapshot_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    snapshot = pd.concat([snapshot, snapshot[snapshot["symbol"] == "600002"]], ignore_index=True)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600002"]


# ---------------------------------------------------------------- 整体闸门


def test_ratio_gate_at_threshold_uses_bulk(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600009", prev_close=1.0)  # 9/10 = 90%
    source = _source(market, snapshot)
    report = _run(source, store)
    assert SNAPSHOT_MIN_MATCH == 0.9
    assert report.bulk_used
    assert _bars_symbols(source) == ["600009"]


def test_ratio_gate_below_threshold_falls_back_for_whole_night(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600008", prev_close=1.0)
    _set(snapshot, "600009", prev_close=1.0)  # 8/10 = 80%
    _set(snapshot, "600000", volume=0.0, open=0.0, close=0.0)  # 无成交的不计入比例
    source = _source(market, snapshot)
    report = _run(source, store)
    assert not report.bulk_used
    assert "昨收" in report.bulk_reason and "7/9" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(10)
    assert (report.bulk_bars, report.per_symbol_bars) == (0, 10)
    assert _snapshot_calls(source) == 1


def test_snapshot_failure_falls_back_and_is_not_retried(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)

    class Throttled(FakeSource):
        def daily_snapshot(self) -> pd.DataFrame:
            self.calls.append(("daily_snapshot", ()))
            raise ConnectionError("模拟被限流")

    source = Throttled(
        trade_days=make_trade_days(CAL_DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in _symbols(3)]),
        bars=market,
        industries=make_industries([(s, "制造业", False) for s in _symbols(3)]),
    )
    report = _run(source, store)
    assert _snapshot_calls(source) == 1
    assert not report.bulk_used
    assert "批量行情获取失败" in report.bulk_reason and "模拟被限流" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(3)
    assert dict(report.failed) == {}


def test_fake_without_snapshot_falls_back_to_per_symbol(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    source = _source(market, None)  # 默认的假数据源不模拟批量行情
    report = _run(source, store)
    assert _snapshot_calls(source) == 1
    assert not report.bulk_used
    assert "NotImplementedError" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(3)
    assert report.per_symbol_bars == 3


def test_malformed_snapshot_falls_back(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    source = _source(market, _snapshot(market).drop(columns=["prev_close"]))
    report = _run(source, store)
    assert not report.bulk_used
    assert "prev_close" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(3)


def test_disabled_by_config(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    cfg = CFG.model_copy(update={"data": CFG.data.model_copy(update={"bulk_bars": False})})
    source = _source(market, _snapshot(market))
    report = update_all(source, store, cfg, AS_OF, now=NOW)
    assert _snapshot_calls(source) == 0
    assert not report.bulk_used and "bulk_bars" in report.bulk_reason
    assert report.per_symbol_bars == 3


def test_without_now_keeps_per_symbol_behaviour(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    source = _source(market, _snapshot(market))
    report = update_all(source, store, CFG, AS_OF)
    assert _snapshot_calls(source) == 0
    assert not report.bulk_used and report.bulk_reason != ""
    assert sorted(_bars_symbols(source)) == _symbols(3)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2024, 1, 12, 15, 30, tzinfo=SH),  # as_of 尚未收盘
        datetime(2024, 1, 15, 9, 0, tzinfo=SH),  # 已到下一个交易日 09:00
        datetime(2024, 1, 15, 20, 0, tzinfo=SH),  # 下一个交易日晚上：as_of 不是最近收盘日
    ],
)
def test_outside_window_falls_back(tmp_path: Path, now: datetime) -> None:
    market = _market(3)
    store = _store(tmp_path, market)
    source = _source(market, _snapshot(market))
    report = _run(source, store, now)
    assert _snapshot_calls(source) == 0
    assert not report.bulk_used and report.bulk_reason != ""
    assert sorted(_bars_symbols(source)) == _symbols(3)


def test_inside_window_on_weekend_and_next_morning(tmp_path: Path) -> None:
    for i, now in enumerate(
        [datetime(2024, 1, 14, 12, 0, tzinfo=SH), datetime(2024, 1, 15, 8, 59, tzinfo=SH)]
    ):
        market = _market(3)
        store = _store(tmp_path / str(i), market)
        report = _run(_source(market, _snapshot(market)), store, now)
        assert report.bulk_used, report.bulk_reason


def test_nothing_needs_bulk_skips_the_request(tmp_path: Path) -> None:
    market = _market(3)
    store = _store(tmp_path, market, through=AS_OF)  # 已是最新
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert _snapshot_calls(source) == 0
    assert not report.bulk_used and report.bulk_reason != ""
    assert (report.updated, report.skipped) == (0, 3)


def test_window_problem_rules() -> None:
    calendar = TradingCalendar(CAL_DAYS)
    assert snapshot_window_problem(NOW, AS_OF, calendar) is None
    assert snapshot_window_problem(datetime(2024, 1, 15, 8, 59, tzinfo=SH), AS_OF, calendar) is None
    assert snapshot_window_problem(datetime(2024, 1, 15, 9, 0, tzinfo=SH), AS_OF, calendar)
    assert snapshot_window_problem(datetime(2024, 1, 12, 15, 59, tzinfo=SH), AS_OF, calendar)
    assert snapshot_window_problem(NOW, PREV, calendar)  # 较早的日子（补跑）
    short = TradingCalendar(DAYS)  # 日历里没有下一个交易日
    assert snapshot_window_problem(NOW, AS_OF, short)
    with pytest.raises(ValueError):
        snapshot_window_problem(datetime.combine(AS_OF, time(20, 0)), AS_OF, calendar)


def test_format_bulk_lines() -> None:
    assert format_bulk(True, 3000, 40, 12, "") == (
        "日线批量行情：已启用，批量写入 3000 只，判为停牌 40 只，逐只请求 12 只"
    )
    assert format_bulk(False, 0, 0, 3487, "批量行情获取失败：超时") == (
        "日线批量行情：未启用（批量行情获取失败：超时），逐只请求 3487 只"
    )


def test_snapshot_schema_columns() -> None:
    assert list(SNAPSHOT_SCHEMA) == [
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "volume",
        "amount",
    ]


# ---------------------------------------------------------------- 复审修订：停牌的认定与整晚闸门


def _zero(snapshot: pd.DataFrame, symbol: str) -> None:
    _set(snapshot, symbol, open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0, amount=0.0)


def test_partly_zeroed_snapshot_falls_back_for_whole_night(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _zero(snapshot, "600001")
    _zero(snapshot, "600002")  # 2/10 = 20% 无成交
    source = _source(market, snapshot)
    report = _run(source, store)
    assert SNAPSHOT_MAX_NO_TRADE == 0.1
    assert not report.bulk_used
    assert "无成交" in report.bulk_reason and "2 只" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(10)
    assert (report.bulk_bars, report.bulk_suspended) == (0, 0)


def test_zero_volume_with_nonzero_price_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _set(snapshot, "600003", volume=0.0)  # 价格不是 0：不像停牌，也不能用
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600003"]
    assert report.bulk_suspended == 0
    assert bar_days(store, "600003") == DAYS


def test_zeroed_row_with_disagreeing_prev_close_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    _zero(snapshot, "600004")
    _set(snapshot, "600004", prev_close=1.0)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600004"]
    assert report.bulk_suspended == 0


def test_small_comparable_sample_falls_back(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    for symbol in _symbols(10)[4:]:  # 6 只落后两天：可比对、有成交的只剩 4/10 < 50%
        own = market[market["symbol"] == symbol]
        store.write(
            Table.BARS, own[own["day"] <= pd.Timestamp(DAYS[-3])].reset_index(drop=True), symbol
        )
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert SNAPSHOT_MIN_COVERAGE == 0.5
    assert not report.bulk_used and "样本太小" in report.bulk_reason
    assert sorted(_bars_symbols(source)) == _symbols(10)


def test_resumed_night_still_uses_snapshot_for_the_rest(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    for symbol in _symbols(10)[4:]:  # 6 只已经有 as_of 的日线（中断后续跑）：不计入分母
        own = market[market["symbol"] == symbol]
        store.write(Table.BARS, own.reset_index(drop=True), symbol)
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == []
    latest = store.read(Table.BARS, start=AS_OF, end=AS_OF)
    assert sorted(latest["symbol"].tolist()) == _symbols(10)


def test_all_zero_pre_open_snapshot_falls_back(tmp_path: Path) -> None:
    market = _market(5)
    store = _store(tmp_path, market)
    snapshot = _snapshot(market)
    for symbol in _symbols(5):
        _zero(snapshot, symbol)
    source = _source(market, snapshot)
    report = _run(source, store)
    assert not report.bulk_used and report.bulk_reason != ""
    assert sorted(_bars_symbols(source)) == _symbols(5)
    assert report.bulk_suspended == 0


def test_dividend_on_as_of_goes_per_symbol(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    div = make_dividends("600005", [(2023, date(2023, 12, 20), AS_OF, 0.001)])
    store.write(Table.DIVIDENDS, div, symbol="600005")
    source = _source(market, _snapshot(market))  # 昨收没变（分红太少，参考价不变）
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600005"]


@pytest.mark.parametrize("factor", [float("nan"), 0.0, -1.0, float("inf")])
def test_bad_stored_adj_factor_goes_per_symbol(tmp_path: Path, factor: float) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    own = store.read(Table.BARS, symbols=["600006"])
    own.loc[own["day"] == pd.Timestamp(PREV), "adj_factor"] = factor
    store.write(Table.BARS, own, symbol="600006")
    source = _source(market, _snapshot(market))
    report = _run(source, store)
    assert report.bulk_used
    assert _bars_symbols(source) == ["600006"]


class _FlakySource(FakeSource):
    """down 里的股票：日线、分红请求都抛网络错误。"""

    down: frozenset[str] = frozenset()

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol in self.down:
            self.calls.append(("bars", (symbol, start, end)))
            raise ConnectionError(f"模拟断网：{symbol}")
        return super().bars(symbol, start, end)

    def dividends(self, symbol: str) -> pd.DataFrame:
        if symbol in self.down:
            self.calls.append(("dividends", (symbol,)))
            raise ConnectionError(f"模拟断网：{symbol}")
        return super().dividends(symbol)


def test_breaker_still_trips_on_bulk_nights(tmp_path: Path) -> None:
    market = _market(10)
    store = _store(tmp_path, market)
    symbols = _symbols(10)
    for symbol in symbols[1::2]:  # 奇数位：批量写入，分红财务当天刚下载过 → 完全不联网
        store.write(Table.DIVIDENDS, make_dividends(symbol, []), symbol=symbol)
        store.write(Table.FINANCIALS, make_financials(symbol, []), symbol=symbol)
        set_file_age(tmp_path, Table.DIVIDENDS, symbol, AS_OF, 0)
        set_file_age(tmp_path, Table.FINANCIALS, symbol, AS_OF, 0)
    lagging = market[market["symbol"] == "600004"]  # 偶数位里有一只要逐只请求日线
    store.write(
        Table.BARS,
        lagging[lagging["day"] <= pd.Timestamp(DAYS[-3])].reset_index(drop=True),
        "600004",
    )
    source = _FlakySource(
        trade_days=make_trade_days(CAL_DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in symbols]),
        bars=market,
        industries=make_industries([(s, "制造业", False) for s in symbols]),
    )
    source.snapshot = _snapshot(market)
    source.down = frozenset(symbols[0::2])
    cfg = CFG.model_copy(
        update={"data": CFG.data.model_copy(update={"max_consecutive_failures": 3})}
    )
    with pytest.raises(NetworkOutageError, match="连续 3 只"):
        update_all(source, store, cfg, AS_OF, now=NOW)
    # 600000、600002（分红）、600004（日线）三次网络失败之间夹着不联网的批量写入股票
    failed_at = [str(a[0]) for n, a in source.calls if n in ("bars", "dividends")]
    assert failed_at == ["600000", "600002", "600004"]
    assert bar_days(store, "600001") == DAYS  # 批量写入照常
