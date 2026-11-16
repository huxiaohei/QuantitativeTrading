"""测试辅助：假数据与默认配置。"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, timedelta
from pathlib import Path

import numpy as np
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
    "dividend_fy_total": 0.52,
    "dividend_prev_fy_total": 0.5,
    "dividend_open_fy_total": 0.0,
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
    snapshot 为批量行情（列见 qt.data.schema.SNAPSHOT_SCHEMA）；为 None 时 daily_snapshot()
    抛 NotImplementedError，更新按"批量行情不可用"处理，退回逐只请求。
    """

    def __init__(self, **tables: pd.DataFrame) -> None:
        names = {t.value for t in Table}
        unknown = set(tables) - names
        if unknown:
            raise ValueError(f"未知表名：{sorted(unknown)}")
        self._tables = tables
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_symbols: set[str] = set()
        self.snapshot: pd.DataFrame | None = None

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

    def daily_snapshot(self) -> pd.DataFrame:
        self.calls.append(("daily_snapshot", ()))
        if self.snapshot is None:
            raise NotImplementedError("假数据源没有模拟批量行情")
        return self.snapshot

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


def snapshot_from_bars(bars: pd.DataFrame, day: date, prev_day: date) -> pd.DataFrame:
    """由日线表构造 day 当天的批量行情。

    开高低收、成交量、成交额取 day 的日线，昨收取 prev_day 的收盘价。

    prev_day 没有日线的股票昨收为 0（与新浪对新股的表示相同）。
    """
    today = bars[bars["day"] == pd.Timestamp(day)].reset_index(drop=True)
    before = bars[bars["day"] == pd.Timestamp(prev_day)]
    prev_close = dict(zip(before["symbol"], before["close"], strict=True))
    return pd.DataFrame(
        {
            "symbol": pd.Series(today["symbol"].tolist(), dtype="str"),
            "open": today["open"].astype("float64"),
            "high": today["high"].astype("float64"),
            "low": today["low"].astype("float64"),
            "close": today["close"].astype("float64"),
            "prev_close": pd.Series(
                [float(prev_close.get(s, 0.0)) for s in today["symbol"]], dtype="float64"
            ),
            "volume": today["volume"].astype("float64"),
            "amount": today["amount"].astype("float64"),
        }
    )


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


DividendRow = tuple[int, date, date, float] | tuple[int, date, date, float, float]


def make_dividends(symbol: str, rows: Sequence[DividendRow]) -> pd.DataFrame:
    """构造 DIVIDENDS 表；每行为 (fiscal_year, announce_date, ex_date, cash_per_share)，
    可选第 5 项 bonus_per_share（每股送转股数，省略时为 0.0）。"""
    bonus = [r[4] if len(r) == 5 else 0.0 for r in rows]
    return pd.DataFrame(
        {
            "symbol": pd.Series([symbol] * len(rows), dtype="str"),
            "fiscal_year": pd.Series([r[0] for r in rows], dtype="int64"),
            "announce_date": _ts([r[1] for r in rows]),
            "ex_date": _ts([r[2] for r in rows]),
            "cash_per_share": pd.Series([r[3] for r in rows], dtype="float64"),
            "bonus_per_share": pd.Series(bonus, dtype="float64"),
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


def make_benchmark(code: str, days: Sequence[date], closes: Sequence[float]) -> pd.DataFrame:
    """构造 BENCHMARKS 表：某个基准代码的每日收盘价。"""
    return pd.DataFrame(
        {
            "code": pd.Series([code] * len(days), dtype="str"),
            "day": _ts(list(days)),
            "close": pd.Series([float(c) for c in closes], dtype="float64"),
        }
    )


def write_calendar(store: ParquetStore, days: Sequence[date]) -> None:
    """把交易日写入 store 的 trade_days 表。"""
    store.write(Table.TRADE_DAYS, pd.DataFrame({"day": _ts(list(days))}))


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


TRADING_START = date(2021, 1, 4)
TRADING_DAYS = 520
TRADING_SYMBOLS = ("600001", "600002", "600003", "600004")
# 各股票价格走势的折点 (交易日序号, 价格)，折点之间线性插值。前 200 天都在 14 元，
# 之后跌到 10 元附近，一年价格位置接近 0，满足买入条件；买入后走势各不相同：
#   600001 缓慢涨到 13.5 元（固定止盈，默认止盈线 20%）；600002 快速跌到 7 元（价格止损，
#   之后在低位横盘，冷却期过后可再次买入）；
#   600003 与 600004 在样本内横盘，第 400 天之后暴跌（样本外才止损）。
_TRADING_KNOTS: dict[str, list[tuple[int, float]]] = {
    "600001": [(0, 14.0), (199, 14.0), (259, 10.0), (262, 10.0), (330, 13.5), (519, 13.5)],
    "600002": [(0, 14.0), (199, 14.0), (259, 10.0), (262, 10.0), (290, 7.0), (519, 7.0)],
    "600003": [(0, 14.0), (199, 14.0), (259, 10.0), (399, 10.0), (430, 7.0), (519, 7.0)],
    "600004": [(0, 14.0), (199, 14.0), (259, 10.2), (399, 10.2), (430, 7.0), (519, 7.0)],
}
_TRADING_INDUSTRIES = {"600001": "银行", "600002": "煤炭", "600003": "电力", "600004": "钢铁"}


def populate_trading_store(store: ParquetStore) -> list[date]:
    """写入一个真实策略会交易的确定性数据集，返回交易日列表。

    4 只股票自 2010 年上市，日成交额 1 亿元；2015 至 2021 财年每年派息 0.6 元（公告于次年 4
    月 20 日），2017 至 2021 财年年报 ROE 10%、每股收益 1 元（分红率 60%）。
    默认配置（回看 250 天、连续分红 5 年等）下，第 250 个交易日前后四只股票同时满足全部买入条件。
    价格走势见 _TRADING_KNOTS：有固定止盈、价格止损，以及样本内结束之后才发生的暴跌。
    """
    days = weekdays(TRADING_START, TRADING_DAYS)
    write_calendar(store, days)
    bars: list[pd.DataFrame] = []
    dividends: list[pd.DataFrame] = []
    financials: list[pd.DataFrame] = []
    for symbol, knots in _TRADING_KNOTS.items():
        xs = [k[0] for k in knots]
        ys = [k[1] for k in knots]
        closes = [round(float(p), 4) for p in np.interp(range(TRADING_DAYS), xs, ys)]
        frame = make_bars(symbol, days, closes)
        frame["amount"] = pd.Series([1e8] * TRADING_DAYS, dtype="float64")
        bars.append(frame)
        dividends.append(
            make_dividends(
                symbol,
                [(fy, date(fy + 1, 4, 20), date(fy + 1, 6, 15), 0.6) for fy in range(2015, 2022)],
            )
        )
        financials.append(
            make_financials(
                symbol,
                [
                    {"report_period": date(fy, 12, 31), "announce_date": date(fy + 1, 4, 20)}
                    for fy in range(2017, 2022)
                ],
            )
        )
    populate_store(
        store,
        securities=make_securities(
            [(s, "sh_main", date(2010, 1, 1), None) for s in TRADING_SYMBOLS]
        ),
        bars=bars,
        dividends=dividends,
        financials=financials,
        industries=make_industries([(s, _TRADING_INDUSTRIES[s], False) for s in TRADING_SYMBOLS]),
    )
    return days
