"""时点正确的每日指标快照（FeaturePanel）。

回测某一天只能看到公告日严格早于该日的信息。本模块一次性按全区间向量化计算所有指标：
    - 价格类指标只用 day 及之前的行（滚动窗口向后看）；
    - 分红、财务类指标先在"事件"上算出状态，再用 merge_asof 对齐到每个交易日，
      公告当天不可见，次日起可见。

分红类指标的财年口径：财年 Y 的分红全部在 Y+1 年 8 月 31 日前除权，故 day > Y+1 年 8 月 31 日
时财年 Y "已关闭"。Y* 为 day 当天最新已关闭财年（9 月起为 day.year-1，否则 day.year-2）。
每股分红一律折算到 day 当天的股本（"当前股本口径"）：一笔分红 d 的每股金额除以
Π(1 + 送转比例)，连乘范围是同一股票除权日在 [d 的除权日, day] 内的全部送转（含 d 自身的送转；
除权日晚于 day 的送转不计，因为 day 的价格尚未反映它）。total(Y, day) 为财年 Y 中公告日 < day
的各笔分红按当前股本口径的合计。
    - dividend_years：自 Y* 起向前、每年 total > 0 的连续财年数（Y* 无分红则为 0）；
    - dividend_cut：total(Y*-1) > 0 且 total(Y*) 低于 total(Y*-1)（Y* 无分红也算下调）；
    - payout_ratio：Y* 的原始每股派息合计（不折算）/ Y* 年报每股收益
      （年报需在 day 前公告且 eps > 0）；
    - dividend_yield：max(total(Y*), total(Y*+1)) / 收盘价，即"最近完整财年"与"当前财年已公告"
      两者中的较大者；削减分红的公司要到被削减的财年关闭后才反映出来（与 dividend_cut 同样滞后）；
    - dividend_fy_total、dividend_prev_fy_total：total(Y*) 与 total(Y*-1)，供卖出说明使用。
未关闭财年的分红（如中期分红）只进入股息率，不进入前三项，避免只公告了部分分红就误判下调。
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
    "dividend_fy_total",
    "dividend_prev_fy_total",
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
_ONE_DAY = pd.Timedelta(days=1)
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


def _consecutive_years(positive: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """对派息为正的 (keys, 财年) 行，算出以该财年结尾的连续财年段长度 run。

    例：某组内财年为 2016、2017、2019、2020，则 run 依次为 1、2、1、2。
    """
    out = positive[[*keys, "fiscal_year"]].sort_values([*keys, "fiscal_year"])
    if out.empty:
        return out.assign(run=pd.Series(dtype="int64"))
    group: npt.NDArray[np.int64] = np.asarray(
        cast(Any, out.groupby(keys, sort=True)).ngroup(), dtype="int64"
    )
    idx: npt.NDArray[np.int64] = np.arange(len(group))
    # 同组内财年严格递增，fy - 组内序号在连续段内恒定
    offset: npt.NDArray[np.int64] = idx - np.searchsorted(group, group, side="left")
    run_key: npt.NDArray[np.int64] = out["fiscal_year"].to_numpy(dtype="int64") - offset
    new_run: npt.NDArray[np.bool_] = np.r_[
        True, (group[1:] != group[:-1]) | (run_key[1:] != run_key[:-1])
    ]
    start: npt.NDArray[np.int64] = np.maximum.accumulate(np.where(new_run, idx, 0))
    return out.assign(run=idx - start + 1)


def _annual_reports(financials: pd.DataFrame) -> pd.DataFrame:
    """年报：报告期末为 12-31 的记录。"""
    period = pd.DatetimeIndex(financials["report_period"])
    return financials[(period.month == 12) & (period.day == 31)]


def _dividend_state(dividends: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """分红事件表与事件后的分年状态（每股金额按事件当天的股本折算）。

    事件有两类，均以"从哪一天起生效"（effective）表示：公告日的次日（该行分红可见），
    以及有送转的行的除权日（股本变化，此前各笔分红需要重新折算）。两次事件之间状态不变。
    对每个 (symbol, effective)，取公告日 < effective 的分红行按财年汇总：
    total 为按当前股本口径折算后的合计，raw 为未折算的原始合计，
    run 为以该财年结尾的连续派息财年数（total 不为正则为 0）。
    返回 (事件表[symbol, effective]，状态表[symbol, effective, fiscal_year, total, raw, run])。
    """
    div = dividends.sort_values(["symbol", "ex_date"], kind="stable").reset_index(drop=True)
    growth = 1.0 + div["bonus_per_share"].fillna(0.0)
    # 截至各行除权日（含）的送转连乘；除以本行自身的 (1 + 送转) 得到本行除权日之前的连乘
    cum = growth.groupby(div["symbol"], sort=False).cumprod()
    div = div.assign(f_before=cum / growth, visible_from=div["announce_date"] + _ONE_DAY)

    bonus_days = div.loc[div["bonus_per_share"] > 0, ["symbol", "ex_date"]]
    events = (
        pd.concat(
            [
                div[["symbol", "visible_from"]].rename(columns={"visible_from": "effective"}),
                bonus_days.rename(columns={"ex_date": "effective"}),
            ],
            ignore_index=True,
        )
        .dropna(subset=["effective"])
        .drop_duplicates()
        .sort_values("effective", kind="stable")
        .reset_index(drop=True)
    )
    factors = pd.DataFrame({"symbol": div["symbol"], "factor_day": div["ex_date"], "f_at": cum})
    factors = factors.dropna(subset=["factor_day"]).sort_values("factor_day", kind="stable")
    # 事件当天（含）以前全部送转的连乘；此前没有任何除权则为 1
    at_event = pd.merge_asof(
        events, factors, left_on="effective", right_on="factor_day", by="symbol"
    ).drop(columns=["factor_day"])
    at_event["f_at"] = at_event["f_at"].fillna(1.0)

    joined = at_event.merge(div, on="symbol")
    joined = joined[joined["visible_from"] <= joined["effective"]]
    applied = (joined["ex_date"] <= joined["effective"]).to_numpy(dtype="bool")
    scale = np.where(applied, _floats(joined["f_before"]) / _floats(joined["f_at"]), 1.0)
    joined = joined.assign(adjusted=_floats(joined["cash_per_share"]) * scale)

    keys = ["symbol", "effective"]
    totals = joined.groupby([*keys, "fiscal_year"], as_index=False).agg(
        total=("adjusted", "sum"), raw=("cash_per_share", "sum")
    )
    runs = _consecutive_years(totals[totals["total"] > 0], keys)
    state = totals.merge(runs, on=[*keys, "fiscal_year"], how="left")
    state["run"] = state["run"].fillna(0).astype("int64")
    return events, state


def _closed_year(days: "pd.Series[pd.Timestamp]") -> npt.NDArray[np.int64]:
    """每个 day 的最新已关闭财年 Y*：财年 Y 在 day > Y+1 年 8 月 31 日后关闭。"""
    index = pd.DatetimeIndex(days)
    after_august = index.month.to_numpy(dtype="int64") >= 9
    return index.year.to_numpy(dtype="int64") - np.where(after_august, 1, 2)


def _state_at(
    state: pd.DataFrame, event_dates: pd.DataFrame, year: npt.NDArray[np.int64]
) -> pd.DataFrame:
    """在每行对应的最近事件上取指定财年的 total、raw 与 run，找不到则为空。"""
    left = pd.DataFrame(
        {
            "symbol": event_dates["symbol"],
            "effective": event_dates["effective"],
            "fiscal_year": year,
        }
    )
    return left.merge(state, on=["symbol", "effective", "fiscal_year"], how="left")


@dataclass(frozen=True)
class _DividendColumns:
    """分红类指标（每行对应 keys 的一行）。"""

    years: npt.NDArray[np.int64]
    cut: npt.NDArray[np.bool_]
    fy_total: Floats  # total(Y*)，当前股本口径
    prev_fy_total: Floats  # total(Y*-1)
    open_total: Floats  # total(Y*+1)：当前未关闭财年已公告的分红
    raw_fy_total: Floats  # Y* 的原始每股派息合计（不折算），用于分红率


def _dividend_columns(keys: pd.DataFrame, dividends: pd.DataFrame) -> _DividendColumns:
    """按 Y* 计算分红类指标；keys 为 (symbol, day)，须按 day 排序。"""
    n = len(keys)
    if dividends.empty:
        zeros = np.zeros(n)
        return _DividendColumns(
            np.zeros(n, dtype="int64"), np.zeros(n, dtype="bool"), zeros, zeros, zeros, zeros
        )
    year = _closed_year(keys["day"])
    events, state = _dividend_state(dividends)
    # 生效日 <= day 的最近一次事件（公告事件的生效日已是公告日次日）
    visible = pd.merge_asof(
        keys, events, left_on="day", right_on="effective", by="symbol", allow_exact_matches=True
    )
    current = _state_at(state, visible, year)
    previous = _state_at(state, visible, year - 1)
    upcoming = _state_at(state, visible, year + 1)
    total = np.nan_to_num(_floats(current["total"]))
    prev_total = np.nan_to_num(_floats(previous["total"]))
    return _DividendColumns(
        years=current["run"].fillna(0).to_numpy(dtype="int64"),
        cut=(prev_total > 0) & (total < prev_total - _CUT_TOLERANCE),
        fy_total=total,
        prev_fy_total=prev_total,
        open_total=np.nan_to_num(_floats(upcoming["total"])),
        raw_fy_total=np.nan_to_num(_floats(current["raw"])),
    )


def _dividend_yield(cols: _DividendColumns, close: Floats) -> Floats:
    """max(total(Y*), total(Y*+1)) / 收盘价；收盘价缺失或不为正时为空。"""
    out = np.full(len(close), np.nan)
    valid = np.isfinite(close) & (close > 0)
    np.divide(np.maximum(cols.fy_total, cols.open_total), close, out=out, where=valid)
    return out


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


def _payout_ratio(keys: pd.DataFrame, total: Floats, annual: pd.DataFrame) -> Floats:
    """Y* 年派息合计 / Y* 年年报每股收益；该年报未在 day 前公告或 eps<=0 为空。"""
    eps = pd.DataFrame(
        {
            "symbol": annual["symbol"],
            "fiscal_year": pd.DatetimeIndex(annual["report_period"]).year.to_numpy(dtype="int64"),
            "eps_announce": annual["announce_date"],
            "eps": annual["eps"],
        }
    )
    left = pd.DataFrame(
        {
            "symbol": keys["symbol"],
            "day": keys["day"],
            "fiscal_year": _closed_year(keys["day"]),
            "total": total,
        }
    )
    joined = left.merge(eps, on=["symbol", "fiscal_year"], how="left")
    usable = (joined["eps_announce"] < joined["day"]) & (joined["eps"] > 0)
    return _floats((joined["total"] / joined["eps"]).where(usable))


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

    div = _dividend_columns(keys, dividends)
    p["dividend_yield"] = _dividend_yield(div, _floats(p["close"]))
    p["dividend_years"] = div.years
    p["dividend_cut"] = div.cut
    p["dividend_fy_total"] = div.fy_total
    p["dividend_prev_fy_total"] = div.prev_fy_total
    annual = _annual_reports(financials)
    # 分红率按 Y* 自身的原始每股口径：每股收益是年报原值，若财年内有送转，两者股本口径可能不一致，
    # 此时分红率只是近似
    p["payout_ratio"] = (
        _payout_ratio(keys, div.raw_fy_total, annual) if not annual.empty else np.nan
    )

    p["profit_yoy"] = np.nan
    p["debt_ratio"] = np.nan
    p["roe_avg3"] = np.nan
    if not financials.empty:
        latest = _asof(keys, _latest_report_state(financials), "announce_date")
        p["profit_yoy"] = _floats(latest["net_profit_yoy"])
        p["debt_ratio"] = _floats(latest["debt_ratio"])
        if not annual.empty:
            p["roe_avg3"] = _floats(
                _asof(keys, _roe_avg3_state(annual), "announce_date")["roe_avg3"]
            )
    return p


def _require_date(day: object) -> None:
    """只接受 datetime.date；datetime、Timestamp 会被静默当成别的日期，必须拒绝。"""
    if type(day) is not date:
        raise TypeError(f"day 必须是 datetime.date，收到 {type(day).__name__}")


class FeaturePanel:
    """全区间预先算好的每日指标；snapshot、bar 只做查表与切片。"""

    def __init__(
        self,
        panel: pd.DataFrame,
        history: pd.DataFrame,
        delistings: dict[date, list[str]],
    ) -> None:
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
        _require_date(day)
        bounds = self._bounds.get(day)
        if bounds is None:
            return self._snapshot.iloc[0:0].copy()
        return self._snapshot.iloc[bounds[0] : bounds[1]].copy()

    def bar(self, day: date, symbol: str) -> Bar | None:
        """当日行情；停牌或无数据返回 None。"""
        _require_date(day)
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
        _require_date(day)
        code = self._code.get(symbol)
        if code is not None:
            lo = int(np.searchsorted(self._hist_codes, code, side="left"))
            hi = int(np.searchsorted(self._hist_codes, code, side="right"))
            n = int(np.searchsorted(self._hist_days[lo:hi], np.datetime64(day, "ns"), side="right"))
            if n > 0:
                return float(self._hist_close[lo + n - 1])
        raise KeyError(f"{symbol} 在 {day} 及之前没有行情")

    def delisted_on(self, day: date) -> list[str]:
        """退市日等于 day 的股票（升序）；day 可以是非交易日，由调用方逐日扫描。"""
        _require_date(day)
        return list(self._delistings.get(day, []))


def build_panel(
    store: ParquetStore, calendar: TradingCalendar, cfg: AppConfig, start: date, end: date
) -> FeaturePanel:
    """读取全部所需历史并构建 [start, end] 的 FeaturePanel。

    日线读到 end 为止的全部历史（保证 start 当天回看窗口有效），分红与财务读全量。
    calendar 参数目前未使用，仅为保持调用方（命令行、测试）的签名不变而保留。
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
    return FeaturePanel(panel, history, delistings)
