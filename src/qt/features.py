"""时点正确的每日指标快照（FeaturePanel）。

回测某一天只能看到公告日严格早于该日的信息。本模块一次性按全区间向量化计算所有指标：
    - 价格类指标只用 day 及之前的行（滚动窗口向后看）；
    - 分红、财务类指标先在"公告事件"上算出状态，再用 merge_asof(allow_exact_matches=False)
      对齐到每个交易日，公告当天不可见，次日起可见。
snapshot 与 bar 只是对预先算好的数据做切片与查表。
"""

import itertools
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore

Floats = npt.NDArray[np.float64]

SNAPSHOT_COLUMNS: list[str] = [
    "close",
    "price_position",
    "dividend_yield",
    "dividend_years",
    "dividend_cut",
    "roe_avg3",
    "payout_ratio",
    "profit_yoy",
    "debt_ratio",
    "industry",
    "is_financial",
    "is_st",
    "listed_years",
    "avg_amount_60d",
]
_AMOUNT_WINDOW = 60
_DIVIDEND_WINDOW_DAYS = 365
# 判断"下调"时的容差（元/股），避免 0.1+0.2 与 0.3 的浮点误差误判为下调
_CUT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Bar:
    """某股票某日的行情。

    prev_close 已按 factor_ratio 调整到当日价格口径；factor_ratio = 当日因子 / 前一交易日因子。
    """

    open: float
    close: float
    prev_close: float
    factor_ratio: float
    is_st: bool


def _floats(series: "pd.Series[Any]") -> Floats:
    return series.to_numpy(dtype="float64", na_value=np.nan)


def _rolling(
    values: "pd.Series[float]",
    position: npt.NDArray[np.integer[Any]],
    window: int,
    how: Literal["max", "min", "mean"],
) -> Floats:
    """按股票分组的滚动统计：对整列滚动，再把窗口跨越股票边界的位置置空。

    position 是行在本股票内的序号；序号不足 window-1 的行窗口里混有别的股票，必须置空。
    """
    roller = values.rolling(window, min_periods=window)
    rolled = roller.max() if how == "max" else roller.min() if how == "min" else roller.mean()
    return np.where(position >= window - 1, _floats(rolled), np.nan)


def _price_columns(bars: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """日线上的价格类指标。bars 必须已按 (symbol, day) 排序且索引为 0..n-1。"""
    grouped = bars.groupby("symbol", sort=False)
    position = grouped.cumcount().to_numpy(dtype="int64")
    adj_close = bars["close"] * bars["adj_factor"]
    high = _rolling(adj_close, position, lookback, "max")
    low = _rolling(adj_close, position, lookback, "min")
    span = high - low
    price_position = np.full(len(bars), np.nan)
    np.divide(_floats(adj_close) - low, span, out=price_position, where=span > 0)

    prev_close = _floats(grouped["close"].shift(1))
    prev_factor = _floats(grouped["adj_factor"].shift(1))
    first = np.isnan(prev_factor)
    ratio = np.where(first, 1.0, _floats(bars["adj_factor"]) / np.where(first, 1.0, prev_factor))
    return bars.assign(
        price_position=price_position,
        avg_amount_60d=_rolling(bars["amount"], position, _AMOUNT_WINDOW, "mean"),
        factor_ratio=ratio,
        # 首行没有前一交易日：前收取当日开盘价
        prev_close=np.where(first, _floats(bars["open"]), prev_close / ratio),
    )


def _st_flags(bars: pd.DataFrame, st: pd.DataFrame) -> npt.NDArray[np.bool_]:
    """每行是否落在该股票任一 ST 区间 [start, end] 内（end 为空表示仍在 ST）。"""
    flags = np.zeros(len(bars), dtype=np.bool_)
    rows = np.flatnonzero(bars["symbol"].isin(st["symbol"]).to_numpy())
    if len(rows) == 0:
        return flags
    sub = pd.DataFrame(
        {"row": rows, "symbol": bars["symbol"].iloc[rows], "day": bars["day"].iloc[rows]}
    )
    merged = sub.merge(st, on="symbol")
    hit = (merged["day"] >= merged["start"]) & (
        merged["end"].isna() | (merged["day"] <= merged["end"])
    )
    flags[merged["row"][hit].to_numpy(dtype="int64")] = True
    return flags


def _asof(left: pd.DataFrame, right: pd.DataFrame, key: str, left_on: str = "day") -> pd.DataFrame:
    """按股票把 right 中 key 严格早于 left_on 的最近一行对齐到 left（公告当天不可见）。"""
    return pd.merge_asof(
        left,
        right.sort_values(key, kind="stable"),
        left_on=left_on,
        right_on=key,
        by="symbol",
        allow_exact_matches=False,
    )


def _dividend_yield(panel: pd.DataFrame, dividends: pd.DataFrame) -> Floats:
    """公告日在 [day-365, day) 内的每股派息合计 / 收盘价；用公告日累计和做两次 asof 相减。"""
    if dividends.empty:
        return np.zeros(len(panel))
    ordered = dividends.sort_values(["symbol", "announce_date"], kind="stable")
    cumulative = ordered.assign(cum=ordered.groupby("symbol")["cash_per_share"].cumsum())
    # 同一公告日多笔时保留累计到最后一笔的值
    table = cumulative.drop_duplicates(["symbol", "announce_date"], keep="last")[
        ["symbol", "announce_date", "cum"]
    ]
    upper = _floats(_asof(panel[["symbol", "day"]], table, "announce_date")["cum"])
    lower_left = panel[["symbol"]].assign(
        lb=panel["day"] - pd.Timedelta(days=_DIVIDEND_WINDOW_DAYS)
    )
    lower = _floats(_asof(lower_left, table, "announce_date", "lb")["cum"])
    return (np.nan_to_num(upper) - np.nan_to_num(lower)) / _floats(panel["close"])


def _consecutive_years(joined: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """每个 (symbol, 事件日) 下，以最近一个派息为正的财年结尾的连续财年段长度。"""
    positive = joined[joined["cash_per_share"] > 0][[*keys, "fiscal_year"]]
    positive = positive.drop_duplicates().sort_values([*keys, "fiscal_year"])
    if positive.empty:
        return pd.DataFrame(
            {**{k: positive[k] for k in keys}, "div_years": pd.Series(dtype="int64")}
        )
    group: npt.NDArray[np.int64] = np.asarray(
        cast(Any, positive.groupby(keys, sort=True)).ngroup(), dtype="int64"
    )
    # 同组内财年严格递增，fy - 组内序号在连续段内恒定
    offset: npt.NDArray[np.int64] = np.arange(len(group)) - np.searchsorted(
        group, group, side="left"
    )
    run_key: npt.NDArray[np.int64] = positive["fiscal_year"].to_numpy(dtype="int64") - offset
    last: npt.NDArray[np.int64] = np.r_[np.flatnonzero(group[1:] != group[:-1]), len(group) - 1]
    in_last_run: npt.NDArray[np.bool_] = run_key == run_key[last][group]
    years: npt.NDArray[np.int64] = np.bincount(group, weights=in_last_run * 1.0).astype("int64")
    out: pd.DataFrame = positive.iloc[last][keys].reset_index(drop=True)
    return out.assign(div_years=years)


def _annual_reports(financials: pd.DataFrame) -> pd.DataFrame:
    """年报：报告期末为 12-31 的记录。"""
    period = pd.DatetimeIndex(financials["report_period"])
    return financials[(period.month == 12) & (period.day == 31)]


def _dividend_state(dividends: pd.DataFrame) -> pd.DataFrame:
    """每个公告事件之后的分红状态：连续分红年数、是否下调、最近财年及其派息合计。

    对 (symbol, 事件日 a) 取公告日 <= a 的全部分红行，状态从 a 之后的交易日起可见。
    """
    events = dividends[["symbol", "announce_date"]].drop_duplicates()
    joined = events.merge(dividends, on="symbol", suffixes=("_ev", ""))
    joined = joined[joined["announce_date"] <= joined["announce_date_ev"]]
    keys = ["symbol", "announce_date_ev"]
    # 每个财年的派息合计（含 0 元行，用于判断最近财年与下调）
    totals = joined.groupby([*keys, "fiscal_year"], as_index=False)["cash_per_share"].sum()
    latest = (
        totals.groupby(keys, as_index=False)["fiscal_year"]
        .max()
        .rename(columns={"fiscal_year": "div_fy"})
    )
    current = latest.merge(totals, left_on=[*keys, "div_fy"], right_on=[*keys, "fiscal_year"])[
        [*keys, "div_fy", "cash_per_share"]
    ].rename(columns={"cash_per_share": "div_total"})
    previous = totals.assign(div_fy=totals["fiscal_year"] + 1).rename(
        columns={"cash_per_share": "prev_total"}
    )[[*keys, "div_fy", "prev_total"]]
    state = current.merge(previous, on=[*keys, "div_fy"], how="left")
    state["div_cut"] = state["prev_total"].notna() & (
        state["div_total"] < state["prev_total"] - _CUT_TOLERANCE
    )
    # 连续分红年数：只看派息为正的财年，取以最近一个正派息财年结尾的连续段长度
    state = state.merge(_consecutive_years(joined, keys), on=keys, how="left")
    state["div_years"] = state["div_years"].fillna(0).astype("int64")
    state["div_fy"] = state["div_fy"].astype("float64")
    return state.rename(columns={"announce_date_ev": "announce_date"})[
        ["symbol", "announce_date", "div_years", "div_cut", "div_fy", "div_total"]
    ]


def _latest_report_state(financials: pd.DataFrame) -> pd.DataFrame:
    """每个公告日之后"期末最晚的已公告报告"的同比与负债率（迟到的旧报告不改变状态）。"""
    ordered = financials.sort_values(["symbol", "announce_date", "report_period"], kind="stable")
    ordered = ordered.drop_duplicates(["symbol", "announce_date"], keep="last")
    newest = ordered.groupby("symbol")["report_period"].cummax()
    return ordered[ordered["report_period"] == newest][
        ["symbol", "announce_date", "net_profit_yoy", "debt_ratio"]
    ]


def _roe_avg3_state(annual: pd.DataFrame) -> pd.DataFrame:
    """每个年报公告日之后：期末最晚的 3 份已公告年报的 ROE 均值，不足 3 份为空。"""
    events = annual[["symbol", "announce_date"]].drop_duplicates()
    joined = events.merge(annual, on="symbol", suffixes=("_ev", ""))
    joined = joined[joined["announce_date"] <= joined["announce_date_ev"]]
    keys = ["symbol", "announce_date_ev"]
    joined = joined.sort_values([*keys, "report_period"], ascending=False, kind="stable")
    top3 = joined[joined.groupby(keys).cumcount() < 3]
    total = top3.groupby(keys)["roe"].sum(min_count=3)
    return (
        (total / 3)
        .rename("roe_avg3")
        .reset_index()
        .rename(columns={"announce_date_ev": "announce_date"})
    )


def _empty_snapshot() -> pd.DataFrame:
    """契约列齐全、类型正确的空快照。"""
    kinds: dict[str, str] = {
        "dividend_years": "int64",
        "dividend_cut": "bool",
        "industry": "str",
        "is_financial": "bool",
        "is_st": "bool",
    }
    cols = {c: pd.Series(dtype=cast(Any, kinds.get(c, "float64"))) for c in SNAPSHOT_COLUMNS}
    return pd.DataFrame(cols, index=pd.Index([], name="symbol", dtype="str"))


def _compute_panel(
    bars: pd.DataFrame,
    securities: pd.DataFrame,
    dividends: pd.DataFrame,
    financials: pd.DataFrame,
    industries: pd.DataFrame,
    st_history: pd.DataFrame,
    lookback: int,
    start: date,
    end: date,
) -> pd.DataFrame:
    """算出区间内每个 (day, symbol) 的全部指标，结果按 (day, symbol) 排序。"""
    full = bars.sort_values(["symbol", "day"], kind="stable").reset_index(drop=True)
    full = _price_columns(full, lookback)
    full = full.assign(is_st=_st_flags(full, st_history))
    in_range = (full["day"] >= pd.Timestamp(start)) & (full["day"] <= pd.Timestamp(end))
    # 滚动窗口已在全历史上算完，这里才裁剪到 [start, end]
    p = full[in_range].sort_values(["day", "symbol"], kind="stable").reset_index(drop=True)

    list_date = securities.set_index("symbol")["list_date"]
    p["listed_years"] = _floats((p["day"] - p["symbol"].map(list_date)).dt.days / 365.25)
    industry = industries.set_index("symbol")
    p["industry"] = p["symbol"].map(industry["industry"]).astype("object").fillna("").astype("str")
    p["is_financial"] = p["symbol"].map(industry["is_financial"]).eq(True).to_numpy(dtype="bool")

    # 公告日缺失的记录无法确定何时可见，一律不使用
    dividends = dividends.dropna(subset=["announce_date"])
    financials = financials.dropna(subset=["announce_date"])
    keys = p[["symbol", "day"]]

    p["dividend_yield"] = _dividend_yield(p, dividends)
    p["dividend_years"] = np.zeros(len(p), dtype="int64")
    p["dividend_cut"] = np.zeros(len(p), dtype="bool")
    p["payout_ratio"] = np.nan
    if not dividends.empty:
        merged = _asof(keys, _dividend_state(dividends), "announce_date")
        p["dividend_years"] = merged["div_years"].fillna(0).to_numpy(dtype="int64")
        p["dividend_cut"] = merged["div_cut"].eq(True).to_numpy(dtype="bool")
        p["payout_ratio"] = _payout_ratio(merged, keys, _annual_reports(financials))

    p["profit_yoy"] = np.nan
    p["debt_ratio"] = np.nan
    p["roe_avg3"] = np.nan
    if not financials.empty:
        latest = _asof(keys, _latest_report_state(financials), "announce_date")
        p["profit_yoy"] = _floats(latest["net_profit_yoy"])
        p["debt_ratio"] = _floats(latest["debt_ratio"])
        annual = _annual_reports(financials)
        if not annual.empty:
            p["roe_avg3"] = _floats(
                _asof(keys, _roe_avg3_state(annual), "announce_date")["roe_avg3"]
            )
    return p


def _payout_ratio(state: pd.DataFrame, keys: pd.DataFrame, annual: pd.DataFrame) -> Floats:
    """最近已公告分红财年的派息合计 / 该财年年报每股收益；年报未公告或 eps<=0 为空。"""
    eps = pd.DataFrame(
        {
            "symbol": annual["symbol"],
            "div_fy": pd.DatetimeIndex(annual["report_period"]).year.to_numpy(dtype="float64"),
            "eps_announce": annual["announce_date"],
            "eps": annual["eps"],
        }
    )
    left = pd.DataFrame(
        {
            "symbol": keys["symbol"],
            "day": keys["day"],
            "div_fy": state["div_fy"],
            "div_total": state["div_total"],
        }
    )
    joined = left.merge(eps, on=["symbol", "div_fy"], how="left")
    usable = (joined["eps_announce"] < joined["day"]) & (joined["eps"] > 0)
    return _floats((joined["div_total"] / joined["eps"]).where(usable))


class FeaturePanel:
    """全区间预先算好的每日指标；snapshot、bar 只做查表与切片。"""

    def __init__(
        self,
        panel: pd.DataFrame,
        history: pd.DataFrame,
        delistings: dict[date, list[str]],
        calendar: TradingCalendar,
    ) -> None:
        self._calendar = calendar
        self._delistings = delistings
        symbols = sorted(set(history["symbol"].tolist()) | set(panel["symbol"].tolist()))
        self._code: dict[str, int] = {s: i for i, s in enumerate(symbols)}

        snap = panel[SNAPSHOT_COLUMNS].copy()
        snap.index = pd.Index(panel["symbol"], name="symbol")
        self._snapshot = snap if len(snap) else _empty_snapshot()
        days = panel["day"].to_numpy(dtype="datetime64[ns]")
        first = np.unique(days, return_index=True)[1]
        edges = [*first.tolist(), len(days)]
        self._bounds: dict[date, tuple[int, int]] = {
            pd.Timestamp(days[lo]).date(): (lo, hi) for lo, hi in itertools.pairwise(edges)
        }
        self._panel_codes = panel["symbol"].map(self._code).to_numpy(dtype="int64")
        self._open = _floats(panel["open"])
        self._close = _floats(panel["close"])
        self._prev_close = _floats(panel["prev_close"])
        self._ratio = _floats(panel["factor_ratio"])
        self._is_st = panel["is_st"].to_numpy(dtype="bool")

        self._hist_codes = history["symbol"].map(self._code).to_numpy(dtype="int64")
        self._hist_days = history["day"].to_numpy(dtype="datetime64[ns]")
        self._hist_close = _floats(history["close"])

    def snapshot(self, day: date) -> pd.DataFrame:
        """day 当日有成交的股票的指标；区间外或无数据返回列齐全的空表。"""
        bounds = self._bounds.get(day)
        if bounds is None:
            return self._snapshot.iloc[0:0].copy()
        return self._snapshot.iloc[bounds[0] : bounds[1]].copy()

    def bar(self, day: date, symbol: str) -> Bar | None:
        """当日行情；停牌或无数据返回 None。"""
        bounds = self._bounds.get(day)
        code = self._code.get(symbol)
        if bounds is None or code is None:
            return None
        lo, hi = bounds
        segment = self._panel_codes[lo:hi]
        i = int(np.searchsorted(segment, code))
        if i >= len(segment) or int(segment[i]) != code:
            return None
        row = lo + i
        return Bar(
            open=float(self._open[row]),
            close=float(self._close[row]),
            prev_close=float(self._prev_close[row]),
            factor_ratio=float(self._ratio[row]),
            is_st=bool(self._is_st[row]),
        )

    def last_close(self, day: date, symbol: str) -> float:
        """截至 day（含）的最后一个收盘价；此前没有任何行情则抛 KeyError。"""
        code = self._code.get(symbol)
        if code is not None:
            lo = int(np.searchsorted(self._hist_codes, code, side="left"))
            hi = int(np.searchsorted(self._hist_codes, code, side="right"))
            n = int(np.searchsorted(self._hist_days[lo:hi], np.datetime64(day, "ns"), side="right"))
            if n > 0:
                return float(self._hist_close[lo + n - 1])
        raise KeyError(f"{symbol} 在 {day} 及之前没有行情")

    def delisted_on(self, day: date) -> list[str]:
        """day 当天退市的股票（升序）；非交易日返回空列表。"""
        if not self._calendar.is_trading_day(day):
            return []
        return list(self._delistings.get(day, []))


def build_panel(
    store: ParquetStore, calendar: TradingCalendar, cfg: AppConfig, start: date, end: date
) -> FeaturePanel:
    """读取全部所需历史并构建 [start, end] 的 FeaturePanel。

    日线读到 end 为止的全部历史（保证 start 当天回看窗口有效），分红与财务读全量。
    """
    securities = store.read(Table.SECURITIES)
    securities = securities[securities["board"].isin(cfg.universe.boards)].reset_index(drop=True)
    symbols = [str(s) for s in securities["symbol"]]
    bars = store.read(Table.BARS, symbols=symbols, end=end)
    delisted = securities.dropna(subset=["delist_date"])
    delistings: dict[date, list[str]] = {}
    for symbol, when in zip(delisted["symbol"], delisted["delist_date"], strict=True):
        delistings.setdefault(pd.Timestamp(when).date(), []).append(str(symbol))
    delistings = {d: sorted(v) for d, v in delistings.items()}

    panel = _compute_panel(
        bars,
        securities,
        store.read(Table.DIVIDENDS, symbols=symbols),
        store.read(Table.FINANCIALS, symbols=symbols),
        store.read(Table.INDUSTRIES),
        store.read(Table.ST_HISTORY),
        cfg.strategy.lookback_days,
        start,
        end,
    )
    history = bars[["symbol", "day", "close"]].sort_values(["symbol", "day"], kind="stable")
    return FeaturePanel(panel, history, delistings, calendar)
