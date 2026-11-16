"""手动账用的行情：用指标面板（FeaturePanel）与本地行情存储（ParquetStore）实现 manual_book.Market。

价格一律为不复权价；分红送转按除权日取自分红表（分红记录晚于除权日才取到时，重放仍按除权日补记）。
同一个对象只在一次运行（一个晚上或一次录入）内使用，分红、行业、证券列表读一次后缓存。
"""

import math
from bisect import bisect_right
from collections.abc import Collection, Sequence
from collections.abc import Set as AbstractSet
from datetime import date

import pandas as pd

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.features import FeaturePanel, build_panel
from qt.service.errors import ValidationError


def _as_date(value: object) -> date | None:
    """表中的日期单元格转为 date；空值（NaT）返回 None。"""
    return value.date() if isinstance(value, pd.Timestamp) else None  # NaT 不是 Timestamp


def _as_float(value: object) -> float:
    """表中的数值单元格转为 float；空值（None、NaN）记为 0。"""
    if not isinstance(value, int | float) or math.isnan(value):  # None、NaN 与非数值记为 0
        return 0.0
    return float(value)


class StoreMarket:
    """行情：收盘价来自指标面板，分红、退市、行业、当日最高最低价来自行情存储。

    只覆盖站点股票池所在板块（与指标面板一致）的股票。
    """

    def __init__(self, store: ParquetStore, panel: FeaturePanel, cfg: AppConfig) -> None:
        self._store = store
        self.panel = panel
        securities = store.read(Table.SECURITIES)
        securities = securities[securities["board"].isin(cfg.universe.boards)]
        symbols = [str(s) for s in securities["symbol"]]
        self._symbols = frozenset(symbols)
        self._delisted: dict[str, date] = {}
        for symbol, when in zip(symbols, securities["delist_date"].tolist(), strict=True):
            day = _as_date(when)
            if day is not None:
                self._delisted[symbol] = day
        industries = store.read(Table.INDUSTRIES)
        self._industry = {
            str(s): str(i)
            for s, i in zip(industries["symbol"], industries["industry"], strict=True)
        }
        self._dividends: dict[str, list[tuple[date, float, float]]] = {}
        self._adj: dict[str, tuple[list[date], list[float]]] = {}

    def _adj_series(self, symbol: str) -> tuple[list[date], list[float]]:
        """某只股票按日期升序的 (日期, 复权收盘价)，只含可用日线；读一次后缓存。"""
        cached = self._adj.get(symbol)
        if cached is not None:
            return cached
        bars = self._store.read(Table.BARS, symbols=[symbol]).sort_values("day")
        days: list[date] = []
        values: list[float] = []
        for ts, close, factor in zip(
            bars["day"].tolist(), bars["close"].tolist(), bars["adj_factor"].tolist(), strict=True
        ):
            day = _as_date(ts)
            value = _as_float(close) * _as_float(factor)
            if day is not None and value > 0:  # 坏数据（非正或缺失）当作没有行情
                days.append(day)
                values.append(value)
        self._adj[symbol] = (days, values)
        return days, values

    def adj_close(self, day: date, symbol: str) -> float | None:
        """day 当天的复权收盘价（收盘价 * 复权因子）；当天没有日线（停牌或数据未到）为 None。"""
        days, values = self._adj_series(symbol)
        i = bisect_right(days, day)
        return values[i - 1] if i > 0 and days[i - 1] == day else None

    def last_adj_close(self, day: date, symbol: str) -> float | None:
        """截至 day（含）最后一个复权收盘价；此前没有任何行情为 None。"""
        days, values = self._adj_series(symbol)
        i = bisect_right(days, day)
        return values[i - 1] if i > 0 else None

    def last_adj_day(self, symbol: str) -> date | None:
        """存储中这只股票最后一个有复权收盘价的日期；完全没有行情为 None。"""
        days, _ = self._adj_series(symbol)
        return days[-1] if days else None

    def known_symbols(self) -> AbstractSet[str]:
        """可以录入的股票代码（站点股票池所在板块的全部证券，含已退市）。"""
        return self._symbols

    def last_close(self, day: date, symbol: str) -> float:
        """截至 day（含）最后一个收盘价：停牌日、或还没有当日数据的日子都取最近一次已知收盘价。"""
        return self.panel.last_close(day, symbol)

    def dividends(self, symbol: str) -> Sequence[tuple[date, float, float]]:
        """(除权日, 每股现金, 每股送转)，按除权日升序；除权日缺失的记录不用。"""
        cached = self._dividends.get(symbol)
        if cached is not None:
            return cached
        df = self._store.read(Table.DIVIDENDS, symbols=[symbol])
        rows: list[tuple[date, float, float]] = []
        for ex, cash, bonus in zip(
            df["ex_date"].tolist(),
            df["cash_per_share"].tolist(),
            df["bonus_per_share"].tolist(),
            strict=True,
        ):
            day = _as_date(ex)
            if day is not None:
                rows.append((day, _as_float(cash), _as_float(bonus)))
        rows.sort(key=lambda r: r[0])
        self._dividends[symbol] = rows
        return rows

    def delisted_before(self, day: date, symbol: str) -> bool:
        """day 时是否已经退市：退市日当天即视为已退市（与回测引擎在退市日清算一致）。"""
        delisted = self._delisted.get(symbol)
        return delisted is not None and delisted <= day

    def industry(self, symbol: str) -> str:
        """所属行业；没有分类时为空字符串（与指标面板一致）。"""
        return self._industry.get(symbol, "")

    def price_range(self, day: date, symbol: str) -> tuple[float, float] | None:
        """当日 (最低价, 最高价)；当日没有日线（停牌或数据未到）返回 None。"""
        bars = self._store.read(Table.BARS, symbols=[symbol], start=day, end=day)
        if bars.empty:
            return None
        return float(bars["low"].min()), float(bars["high"].max())


def security_names(store: ParquetStore) -> dict[str, str]:
    """股票代码 -> 名称（证券表里没有的不在其中）。"""
    secs = store.read(Table.SECURITIES)
    return {str(s): str(n) for s, n in zip(secs["symbol"], secs["name"], strict=True)}


_EARLIEST = date(1990, 1, 1)


def build_store_market(
    store: ParquetStore,
    cfg: AppConfig,
    calendar: TradingCalendar,
    today: date,
    symbols: Collection[str] | None = None,
) -> StoreMarket:
    """行情：以不晚于今天的最近一个交易日为指标面板的日期。

    symbols 为 None 时面板覆盖站点股票池的全部股票；否则只覆盖这些股票（内存小得多），
    它们的收盘价等结果与全量面板逐项相同。
    """
    days = calendar.between(_EARLIEST, today)
    if not days:
        raise ValidationError(f"交易日历里没有 {today} 及以前的交易日")
    panel = build_panel(store, calendar, cfg, days[-1], days[-1], symbols)
    return StoreMarket(store, panel, cfg)
