"""测试辅助：假数据与默认配置。"""

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pandas as pd

from qt.config import CostsConfig, RiskConfig, load_config
from qt.data.schema import Table, empty_frame
from qt.models import BuyCandidate, Position

_ROOT = Path(__file__).resolve().parent.parent


def default_costs() -> CostsConfig:
    """返回 config/default.yaml 中的 costs 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").costs


def default_risk() -> RiskConfig:
    """返回 config/default.yaml 中的 risk 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").risk


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
