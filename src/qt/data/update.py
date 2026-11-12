"""增量更新与数据完整性检查。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

import pandas as pd

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore

# 已退市股票取不到日线时，failed 里的错误信息以此开头，便于统计
NO_BARS_PREFIX = "无日线数据"
# 日线首次下载往前多取的年数（供 250 日回看使用）
_LOOKBACK_YEARS = 2
# 当日有日线的股票占在市股票的最低比例
_MIN_COVERAGE = 0.8
# 复权后收盘价单日涨跌幅的上限
_MAX_JUMP = 0.21
# 上市后前 N 个交易日不检查涨跌幅（新股上市初期涨跌幅限制不同）
_IPO_EXEMPT_DAYS = 5


class DataIntegrityError(Exception):
    """数据缺失或不完整，无法继续。"""


class SharedTableError(Exception):
    """共享表（交易日历、证券列表、行业、ST、基准）更新失败。"""


@dataclass(frozen=True)
class UpdateReport:
    """一次更新的结果。

    updated/skipped 按日线计：有新日线写入记为 updated，其余（日线已最新、已退市且已有日线、
    退市早于下载起点、新股尚无行情）记为 skipped。failed 为 symbol -> 错误信息。
    """

    updated: int
    skipped: int
    failed: Mapping[str, str]


def count_no_bars(failed: Mapping[str, str]) -> int:
    """failed 中“已退市且取不到日线”的股票数。"""
    return sum(1 for message in failed.values() if message.startswith(NO_BARS_PREFIX))


class _NoBarsError(Exception):
    """已退市股票没有取到任何日线。"""


def load_calendar(store: ParquetStore) -> TradingCalendar:
    """由已存储的 trade_days 表构建交易日历；表为空时报错。"""
    days = store.read(Table.TRADE_DAYS)
    if len(days) == 0:
        raise DataIntegrityError("trade_days 表为空，请先运行 qt data update")
    return TradingCalendar([ts.date() for ts in days["day"]])


def latest_trading_day(calendar: TradingCalendar, today: date) -> date:
    """不晚于 today 的最近一个交易日；日历里没有则报错。"""
    days = calendar.between(date(1990, 1, 1), today)
    if not days:
        raise DataIntegrityError(f"交易日历中没有 {today} 及以前的交易日")
    return days[-1]


def _shared[T](name: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except Exception as exc:
        raise SharedTableError(f"{name} 更新失败：{type(exc).__name__}: {exc}") from exc


def _refresh_shared(source: DataSource, store: ParquetStore, cfg: AppConfig, today: date) -> None:
    """每次运行刷新一次的共享表；任何一张失败都抛 SharedTableError。"""
    _shared("trade_days", lambda: store.write(Table.TRADE_DAYS, source.trade_days()))

    def write_securities() -> None:
        secs = source.securities()
        store.write(Table.SECURITIES, secs[secs["board"].isin(cfg.universe.boards)])

    _shared("securities", write_securities)
    _shared("industries", lambda: store.write(Table.INDUSTRIES, source.industries()))
    _shared("st_history", lambda: store.write(Table.ST_HISTORY, source.st_history()))

    def write_benchmarks() -> None:
        frames = [
            source.benchmark(code, cfg.backtest.data_start, today)
            for code in cfg.backtest.benchmarks
        ]
        merged = pd.concat(frames, ignore_index=True)
        days = store.read(Table.TRADE_DAYS)
        if len(days) > 0:  # 中证指数含非交易日的行（如 2009-01-01），按交易日历过滤
            merged = merged[merged["day"].isin(days["day"])].reset_index(drop=True)
        store.write(Table.BENCHMARKS, merged)

    _shared("benchmarks", write_benchmarks)


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 2 月 29 日
        return day.replace(year=day.year - years, day=28)


def _is_stale(
    store: ParquetStore, table: Table, symbol: str, today: date, refresh_days: int
) -> bool:
    """文件不存在，或修改时间距今超过 refresh_days 天。"""
    stamp = store.modified_time(table, symbol)
    return stamp is None or (today - stamp.date()).days > refresh_days


def _update_bars(
    source: DataSource,
    store: ParquetStore,
    cfg: AppConfig,
    symbol: str,
    delisted: bool,
    today: date,
    latest: date,
) -> bool:
    """更新一只股票的日线；有新行写入返回 True。"""
    first_start = _years_before(cfg.backtest.in_sample_start, _LOOKBACK_YEARS)
    last = store.last_day(Table.BARS, symbol)
    if delisted and last is not None:
        return False  # 已退市股票的日线不会再变
    if last is not None and last >= latest:
        return False
    start = first_start if last is None else last + timedelta(days=1)
    df = source.bars(symbol, start, today)
    if len(df) == 0:
        if delisted and last is None:
            raise _NoBarsError
        return False
    store.append(Table.BARS, df, symbol)
    return True


def _update_fundamentals(
    source: DataSource, store: ParquetStore, cfg: AppConfig, symbol: str, today: date
) -> None:
    """分红与财务：文件缺失或过期才重新下载，只保留 data_start 之后的行。"""
    days = cfg.data.fundamentals_refresh_days
    start = pd.Timestamp(cfg.backtest.data_start)
    if _is_stale(store, Table.DIVIDENDS, symbol, today, days):
        div = source.dividends(symbol)
        store.write(Table.DIVIDENDS, div[div["ex_date"] >= start], symbol)
    if _is_stale(store, Table.FINANCIALS, symbol, today, days):
        fin = source.financials(symbol)
        store.write(Table.FINANCIALS, fin[fin["report_period"] >= start], symbol)


def update_all(
    source: DataSource,
    store: ParquetStore,
    cfg: AppConfig,
    today: date,
    symbols: Sequence[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> UpdateReport:
    """更新全部数据。

    先刷新共享表（失败抛 SharedTableError），再逐只股票更新日线、分红、财务。
    symbols 非空时只处理这些股票的逐股数据。单只股票失败记入 failed，不影响其他股票。
    progress(已处理数, 总数) 每处理 100 只调用一次。
    """
    _refresh_shared(source, store, cfg, today)
    secs = store.read(Table.SECURITIES)
    in_universe = secs[secs["board"].isin(cfg.universe.boards)].set_index("symbol")
    wanted = list(in_universe.index) if symbols is None else list(dict.fromkeys(symbols))

    calendar_days = store.read(Table.TRADE_DAYS)
    latest = today
    if len(calendar_days) > 0:
        latest = latest_trading_day(load_calendar(store), today)
    first_start = _years_before(cfg.backtest.in_sample_start, _LOOKBACK_YEARS)

    updated = skipped = 0
    failed: dict[str, str] = {}
    delist_fix: dict[str, pd.Timestamp] = {}
    for i, symbol in enumerate(wanted, start=1):
        try:
            if symbol not in in_universe.index:
                raise ValueError("不在配置板块的证券列表中")
            delist = cast(pd.Timestamp, in_universe.loc[symbol, "delist_date"])
            delisted = not pd.isna(delist)
            if delisted and delist < pd.Timestamp(first_start):
                skipped += 1  # 退市早于下载起点，窗口内不可能有日线
            else:
                changed = _update_bars(source, store, cfg, symbol, delisted, today, latest)
                _update_fundamentals(source, store, cfg, symbol, today)
                if delisted:
                    last = store.last_day(Table.BARS, symbol)
                    if last is not None and pd.Timestamp(last) < delist:
                        delist_fix[symbol] = pd.Timestamp(last)  # 取 min(退市表日期, 最后一根日线)
                updated, skipped = (updated + 1, skipped) if changed else (updated, skipped + 1)
        except _NoBarsError:
            failed[symbol] = f"{NO_BARS_PREFIX}：已退市股票取不到日线"
        except Exception as exc:  # 单只股票失败不影响其他股票
            failed[symbol] = f"{type(exc).__name__}: {exc}"
        if progress is not None and i % 100 == 0:
            progress(i, len(wanted))

    if delist_fix:
        fixed = secs.copy()
        for symbol, last_ts in delist_fix.items():
            fixed.loc[fixed["symbol"] == symbol, "delist_date"] = last_ts
        store.write(Table.SECURITIES, fixed)
    return UpdateReport(updated=updated, skipped=skipped, failed=failed)


def _ipo_exempt_until(calendar: TradingCalendar, list_date: pd.Timestamp) -> date | None:
    """上市后前 5 个交易日（含上市日）的最后一天；list_date 缺失返回 None。"""
    if pd.isna(list_date):
        return None
    listed = list_date.date()
    steps = _IPO_EXEMPT_DAYS - 1 if calendar.is_trading_day(listed) else _IPO_EXEMPT_DAYS
    try:
        return calendar.offset(listed, steps)
    except ValueError:  # 超出日历范围
        return date.max


def check_integrity(store: ParquetStore, calendar: TradingCalendar, day: date) -> list[str]:
    """检查数据完整性，返回问题描述列表，空列表表示正常。

    1. day 是交易日，但当日有日线的股票少于在市股票的 80%；
    2. 某只股票复权后（close * adj_factor）收盘价单日涨跌幅绝对值超过 21%，
       上市后前 5 个交易日除外。除权除息日的未复权价格跳变因此不会误报。
    """
    secs = store.read(Table.SECURITIES)
    if len(secs) == 0:
        return ["securities 表为空，无法检查"]
    problems: list[str] = []
    if calendar.is_trading_day(day):
        ts = pd.Timestamp(day)
        listed = secs[
            (secs["list_date"] <= ts) & (secs["delist_date"].isna() | (secs["delist_date"] >= ts))
        ]
        have = store.read(Table.BARS, start=day, end=day)
        covered = set(have["symbol"]) & set(listed["symbol"])
        if len(listed) > 0 and len(covered) < _MIN_COVERAGE * len(listed):
            problems.append(
                f"{day} 仅 {len(covered)}/{len(listed)} 只在市股票有日线，低于 {_MIN_COVERAGE:.0%}"
            )
    for symbol, list_date in zip(secs["symbol"], secs["list_date"], strict=True):
        bars = store.read(Table.BARS, symbols=[symbol])
        if len(bars) < 2:
            continue
        hfq = bars["close"] * bars["adj_factor"]
        change = hfq.pct_change()
        exempt_until = _ipo_exempt_until(calendar, list_date)
        for when, pct in zip(bars["day"], change, strict=True):
            if pd.isna(pct) or abs(pct) <= _MAX_JUMP:
                continue
            if exempt_until is not None and when.date() <= exempt_until:
                continue
            problems.append(f"{symbol} 在 {when.date()} 复权收盘价单日变动 {pct:+.1%}")
    return problems
