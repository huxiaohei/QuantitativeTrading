"""测试辅助：假数据与默认配置。"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from qt.config import (
    AppConfig,
    CostsConfig,
    RiskConfig,
    StrategyConfig,
    UniverseConfig,
    load_config,
)
from qt.data.schema import SCHEMAS, Table, empty_frame
from qt.data.store import ParquetStore
from qt.models import BuyCandidate, ExitReason, Position, SellSignal

_ROOT = Path(__file__).resolve().parent.parent


def default_app_config() -> AppConfig:
    """返回 config/default.yaml 对应的完整配置。"""
    return load_config(_ROOT / "config" / "default.yaml")


def default_costs() -> CostsConfig:
    """返回 config/default.yaml 中的 costs 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").costs


def default_risk() -> RiskConfig:
    """返回 config/default.yaml 中的 risk 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").risk


def default_strategy_config() -> StrategyConfig:
    """返回 config/default.yaml 中的 strategy 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").strategy


def default_universe_config() -> UniverseConfig:
    """返回 config/default.yaml 中的 universe 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").universe


# 默认行：满足全部买入条件，测试只覆盖要测的列
_SNAPSHOT_DEFAULTS: dict[str, object] = {
    "close": 10.0,
    "price_position": 0.12,
    "dividend_yield": 0.052,
    "dividend_years": 8,
    "dividend_cut": False,
    "roe_avg3": 0.12,
    "payout_ratio": 0.5,
    "profit_yoy": 0.05,
    "debt_ratio": 0.5,
    "industry": "银行",
    "is_financial": False,
    "is_st": False,
    "listed_years": 10.0,
    "avg_amount_60d": 1e8,
}


def make_snapshot(rows: Mapping[str, Mapping[str, object]]) -> pd.DataFrame:
    """构造 snapshot：以 symbol 为索引，列与任务 6 的 snapshot 契约一致。

    每行先取默认值（通过全部买入条件），再用 rows 中给出的列覆盖。
    """
    records = [{**_SNAPSHOT_DEFAULTS, **overrides} for overrides in rows.values()]
    df = pd.DataFrame(
        records, index=pd.Index(list(rows), name="symbol"), columns=list(_SNAPSHOT_DEFAULTS)
    )
    for col in ("dividend_cut", "is_financial", "is_st"):
        df[col] = df[col].astype(bool)
    return df


def make_candidate(
    symbol: str, close: float = 10.0, industry: str = "银行", day: date = date(2024, 1, 2)
) -> BuyCandidate:
    """构造买入候选。"""
    return BuyCandidate(
        symbol=symbol, day=day, close=close, industry=industry, metrics={}, reason="测试买入"
    )


def make_position(
    symbol: str,
    shares: float,
    cost: float,
    industry: str = "银行",
    entry_day: date = date(2024, 1, 2),
) -> Position:
    """构造持仓。"""
    return Position(
        symbol=symbol,
        shares=shares,
        cost=cost,
        entry_day=entry_day,
        industry=industry,
        entry_metrics={},
    )


def make_bars(
    symbol: str,
    days: Sequence[date],
    closes: Sequence[float],
    adj_factors: Sequence[float] | None = None,
) -> pd.DataFrame:
    """构造合法的 BARS 表：开高低收相同，成交量 1_000_000。"""
    n = len(days)
    volume = 1_000_000.0
    factors = [1.0] * n if adj_factors is None else list(adj_factors)
    close = [float(c) for c in closes]
    return pd.DataFrame(
        {
            "symbol": pd.Series([symbol] * n, dtype="str"),
            "day": pd.to_datetime(list(days)).astype("datetime64[ns]"),
            "open": pd.Series(close, dtype="float64"),
            "high": pd.Series(close, dtype="float64"),
            "low": pd.Series(close, dtype="float64"),
            "close": pd.Series(close, dtype="float64"),
            "volume": pd.Series([volume] * n, dtype="float64"),
            "amount": pd.Series([c * volume for c in close], dtype="float64"),
            "adj_factor": pd.Series(factors, dtype="float64"),
        }
    )


def make_ohlc_bars(
    symbol: str,
    days: Sequence[date],
    opens: Sequence[float],
    closes: Sequence[float],
    adj_factors: Sequence[float] | None = None,
) -> pd.DataFrame:
    """构造开盘价与收盘价可以不同的 BARS 表（回测撮合用开盘价，计值用收盘价）。"""
    bars = make_bars(symbol, days, closes, adj_factors)
    bars["open"] = pd.Series([float(o) for o in opens], dtype="float64")
    return bars


class ScriptedStrategy:
    """脚本化桩策略：只在配置的日期给出买入候选或卖出信号。

    buys: 日期 -> 当日买入候选；sells: 日期 -> [(股票, 卖出原因)]，仅当该股票当时持仓才发信号。
    held_seen 记录每次 buy_candidates 收到的 held 参数，供测试核对。
    """

    name = "scripted"

    def __init__(
        self,
        buys: Mapping[date, Sequence[BuyCandidate]] | None = None,
        sells: Mapping[date, Sequence[tuple[str, ExitReason]]] | None = None,
    ) -> None:
        self._buys = buys or {}
        self._sells = sells or {}
        self.held_seen: list[tuple[date, frozenset[str]]] = []

    def buy_candidates(
        self, day: date, snapshot: pd.DataFrame, held: AbstractSet[str]
    ) -> list[BuyCandidate]:
        self.held_seen.append((day, frozenset(held)))
        return [c for c in self._buys.get(day, ()) if c.symbol not in held]

    def sell_signals(
        self, day: date, snapshot: pd.DataFrame, positions: Sequence[Position]
    ) -> list[SellSignal]:
        owned = {p.symbol for p in positions}
        return [
            SellSignal(symbol, day, reason, "测试卖出")
            for symbol, reason in self._sells.get(day, ())
            if symbol in owned
        ]


def _by_symbol(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    return df[df["symbol"] == symbol].reset_index(drop=True)


def _by_range(df: pd.DataFrame, col: str, start: date, end: date) -> pd.DataFrame:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    mask = (df[col] >= lo) & (df[col] <= hi)
    return df[mask].reset_index(drop=True)


class FakeSource:
    """实现 DataSource 的假数据源；未提供的表返回空表。

    calls 记录 (方法名, 参数)；fail_symbols 中的股票在按股票取数时抛 RuntimeError。
    """

    def __init__(self, **tables: pd.DataFrame) -> None:
        names = {t.value for t in Table}
        unknown = set(tables) - names
        if unknown:
            raise ValueError(f"未知表名：{sorted(unknown)}")
        self._tables = tables
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_symbols: set[str] = set()

    def _get(self, table: Table) -> pd.DataFrame:
        return self._tables.get(table.value, empty_frame(table))

    def _check(self, symbol: str) -> None:
        if symbol in self.fail_symbols:
            raise RuntimeError(f"模拟取数失败：{symbol}")

    def trade_days(self) -> pd.DataFrame:
        self.calls.append(("trade_days", ()))
        return self._get(Table.TRADE_DAYS)

    def securities(self) -> pd.DataFrame:
        self.calls.append(("securities", ()))
        return self._get(Table.SECURITIES)

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append(("bars", (symbol, start, end)))
        self._check(symbol)
        return _by_range(_by_symbol(self._get(Table.BARS), symbol), "day", start, end)

    def dividends(self, symbol: str) -> pd.DataFrame:
        self.calls.append(("dividends", (symbol,)))
        self._check(symbol)
        return _by_symbol(self._get(Table.DIVIDENDS), symbol)

    def financials(self, symbol: str) -> pd.DataFrame:
        self.calls.append(("financials", (symbol,)))
        self._check(symbol)
        return _by_symbol(self._get(Table.FINANCIALS), symbol)

    def industries(self) -> pd.DataFrame:
        self.calls.append(("industries", ()))
        return self._get(Table.INDUSTRIES)

    def st_history(self) -> pd.DataFrame:
        self.calls.append(("st_history", ()))
        return self._get(Table.ST_HISTORY)

    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append(("benchmark", (code, start, end)))
        df = self._get(Table.BENCHMARKS)
        return _by_range(df[df["code"] == code].reset_index(drop=True), "day", start, end)


def weekdays(start: date, n: int) -> list[date]:
    """从 start 起的连续 n 个工作日（周一至周五），测试里当作交易日。"""
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _ts(values: Sequence[date | None]) -> pd.Series:
    """日期列表转 datetime64[ns] 列，None 变 NaT。"""
    return pd.Series(
        [pd.NaT if v is None else pd.Timestamp(v) for v in values], dtype="datetime64[ns]"
    )


def make_securities(rows: Sequence[tuple[str, str, date, date | None]]) -> pd.DataFrame:
    """构造 SECURITIES 表；每行为 (symbol, board, list_date, delist_date)。"""
    return pd.DataFrame(
        {
            "symbol": pd.Series([r[0] for r in rows], dtype="str"),
            "name": pd.Series([f"股票{r[0]}" for r in rows], dtype="str"),
            "board": pd.Series([r[1] for r in rows], dtype="str"),
            "list_date": _ts([r[2] for r in rows]),
            "delist_date": _ts([r[3] for r in rows]),
        }
    )


def make_dividends(symbol: str, rows: Sequence[tuple[int, date, date, float]]) -> pd.DataFrame:
    """构造 DIVIDENDS 表；每行为 (fiscal_year, announce_date, ex_date, cash_per_share)。"""
    return pd.DataFrame(
        {
            "symbol": pd.Series([symbol] * len(rows), dtype="str"),
            "fiscal_year": pd.Series([r[0] for r in rows], dtype="int64"),
            "announce_date": _ts([r[1] for r in rows]),
            "ex_date": _ts([r[2] for r in rows]),
            "cash_per_share": pd.Series([r[3] for r in rows], dtype="float64"),
        }
    )


_FINANCIAL_DEFAULTS: dict[str, float] = {
    "roe": 0.10,
    "net_profit": 1e9,
    "net_profit_yoy": 0.10,
    "debt_ratio": 0.50,
    "eps": 1.0,
}


def make_financials(symbol: str, rows: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    """构造 FINANCIALS 表；每行必须给 report_period 与 announce_date，其余列有默认值。"""

    def num(row: Mapping[str, object], col: str) -> float:
        return float(str(row.get(col, _FINANCIAL_DEFAULTS[col])))

    def when(row: Mapping[str, object], col: str) -> date:
        value = row[col]
        assert isinstance(value, date)
        return value

    data: dict[str, pd.Series] = {
        "symbol": pd.Series([symbol] * len(rows), dtype="str"),
        "report_period": _ts([when(r, "report_period") for r in rows]),
        "announce_date": _ts([when(r, "announce_date") for r in rows]),
    }
    for col in _FINANCIAL_DEFAULTS:
        data[col] = pd.Series([num(r, col) for r in rows], dtype="float64")
    return pd.DataFrame(data)[list(SCHEMAS[Table.FINANCIALS])]


def make_industries(rows: Sequence[tuple[str, str, bool]]) -> pd.DataFrame:
    """构造 INDUSTRIES 表；每行为 (symbol, industry, is_financial)。"""
    return pd.DataFrame(
        {
            "symbol": pd.Series([r[0] for r in rows], dtype="str"),
            "industry": pd.Series([r[1] for r in rows], dtype="str"),
            "is_financial": pd.Series([r[2] for r in rows], dtype="bool"),
        }
    )


def make_st_history(rows: Sequence[tuple[str, date, date | None]]) -> pd.DataFrame:
    """构造 ST_HISTORY 表；每行为 (symbol, start, end)，end 为 None 表示仍在 ST。"""
    return pd.DataFrame(
        {
            "symbol": pd.Series([r[0] for r in rows], dtype="str"),
            "start": _ts([r[1] for r in rows]),
            "end": _ts([r[2] for r in rows]),
        }
    )


def populate_store(
    store: ParquetStore,
    *,
    securities: pd.DataFrame | None = None,
    bars: Sequence[pd.DataFrame] = (),
    dividends: Sequence[pd.DataFrame] = (),
    financials: Sequence[pd.DataFrame] = (),
    industries: pd.DataFrame | None = None,
    st_history: pd.DataFrame | None = None,
) -> None:
    """把测试表写入 store；bars、dividends、financials 为每只股票一个 DataFrame。"""
    if securities is not None:
        store.write(Table.SECURITIES, securities)
    if industries is not None:
        store.write(Table.INDUSTRIES, industries)
    if st_history is not None:
        store.write(Table.ST_HISTORY, st_history)
    for table, frames in (
        (Table.BARS, bars),
        (Table.DIVIDENDS, dividends),
        (Table.FINANCIALS, financials),
    ):
        for frame in frames:
            store.write(table, frame, symbol=str(frame["symbol"].iloc[0]))
