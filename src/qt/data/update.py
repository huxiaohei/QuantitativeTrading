"""增量更新与数据完整性检查。"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.akshare_convert import build_industries
from qt.data.delist import finalize_securities
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore

# 已退市股票取不到日线时，failed 里的错误信息以此开头，便于统计
NO_BARS_PREFIX = "无日线数据"
# 退市表没有退市日期且没有日线、因此被丢弃的证券
DELIST_MISSING_PREFIX = "退市日期缺失"
# 某只股票行业查询失败
INDUSTRY_FAILED_PREFIX = "行业查询失败"
# 日线首次下载往前多取的年数（供 250 日回看使用）
_LOOKBACK_YEARS = 2
# 当日有日线的股票占在市股票的最低比例
_MIN_COVERAGE = 0.8
# 复权后收盘价单日涨跌幅的上限
_MAX_JUMP = 0.21
# 上市后前 N 个交易日不检查涨跌幅（新股上市初期涨跌幅限制不同）
_IPO_EXEMPT_DAYS = 5
# 已退市股票最后一根日线早于 as_of 超过这么多天，视为早已结束，不再请求
_LONG_DEAD_DAYS = 365
# 沪市退市表的日期是“暂停上市日期”，可能早于最后交易日约一个月；判断“退市早于下载窗口”时留出余量
_DELIST_MARGIN_DAYS = 90
# A 股收盘时间（上海时间）
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = time(16, 0)

ProgressFn = Callable[[str, int, int], None]
# 进度回调的默认间隔（只）
DEFAULT_PROGRESS_EVERY = 20

_log = logging.getLogger(__name__)


class DataIntegrityError(Exception):
    """数据缺失或不完整，无法继续。"""


class SharedTableError(Exception):
    """共享表（交易日历、证券列表、ST、基准）更新失败。"""


class NetworkOutageError(Exception):
    """连续多只股票因网络错误失败，判断为网络中断，中止本次运行（已下载的数据保留，可续传）。"""


def _is_network_error(exc: BaseException) -> bool:
    """网络类错误：连接、超时与 requests 的各种异常都是 OSError 的子类。"""
    return isinstance(exc, OSError)


class _OutageGuard:
    """统计连续因网络错误失败的股票数，达到上限即抛 NetworkOutageError。"""

    def __init__(self, limit: int, stage: str) -> None:
        self._limit = limit
        self._stage = stage
        self._streak = 0
        self._last = ""

    def ok(self) -> None:
        """本只股票没有因网络错误失败（成功、跳过或其他原因失败）。"""
        self._streak = 0

    def failed(self, exc: BaseException) -> None:
        if not _is_network_error(exc):
            self._streak = 0
            return
        self._streak += 1
        self._last = f"{type(exc).__name__}: {exc}"
        if self._streak >= self._limit:
            raise NetworkOutageError(
                f"{self._stage}阶段连续 {self._streak} 只股票因网络错误失败"
                f"（最近一次：{self._last}），判断为网络中断，已中止本次运行；"
                "已下载的数据已保存，网络恢复后重新运行即可续传"
            )


@runtime_checkable
class IndustryLookup(Protocol):
    """可逐只查询行业的数据源（可选能力）：None 表示没有分类，查询失败抛异常。"""

    def industry(self, symbol: str) -> tuple[str, bool] | None: ...


@runtime_checkable
class AsOfAware(Protocol):
    """需要知道本次运行 as_of 的数据源（可选能力）。"""

    def set_as_of(self, as_of: date) -> None: ...


@dataclass(frozen=True)
class UpdateReport:
    """一次更新的结果。

    updated/skipped 按日线计：有新日线写入记为 updated，其余（日线已最新、已退市且太久没有新数据、
    退市早于下载起点、新股尚无行情）记为 skipped。failed 为 symbol -> 错误信息；
    problems 是需要让命令行以非零码退出的数据问题（如行业覆盖率不足）。
    """

    updated: int
    skipped: int
    failed: Mapping[str, str]
    problems: Sequence[str] = ()


def count_no_bars(failed: Mapping[str, str]) -> int:
    """failed 中“已退市且取不到日线”的股票数。"""
    return sum(1 for message in failed.values() if NO_BARS_PREFIX in message)


def count_delist_missing(failed: Mapping[str, str]) -> int:
    """failed 中“退市日期缺失而被丢弃”的证券数。"""
    return sum(1 for message in failed.values() if DELIST_MISSING_PREFIX in message)


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


def latest_closed_day(now: datetime, calendar: TradingCalendar) -> date:
    """最近一个已完整收盘的交易日。

    now 必须带时区，按上海时间解释：今天是交易日且已到 16:00 返回今天，
    否则返回严格早于今天的最近一个交易日。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须是带时区的时间")
    local = now.astimezone(_MARKET_TZ)
    today = local.date()
    if calendar.is_trading_day(today) and local.time() >= _MARKET_CLOSE:
        return today
    return latest_trading_day(calendar, today - timedelta(days=1))


def _shared[T](name: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except Exception as exc:
        raise SharedTableError(f"{name} 更新失败：{type(exc).__name__}: {exc}") from exc


def merge_st_history(stored: pd.DataFrame, current: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """把当前 ST 名单并入已存储的 st_history，保留以前确认过的 ST 区间。

    - 当前是 ST 且已有未结束的区间：保留该区间（原起点）；否则新开一个区间，start = as_of；
    - 有未结束的区间但当前不再是 ST：end = as_of；
    - 已结束的区间原样保留（摘帽后再次被标记则新增第二个区间）。
    """
    stamp = pd.Timestamp(as_of)
    now_st = {str(s) for s in current["symbol"]}
    if not now_st and stored["end"].isna().any():
        raise ValueError(
            "数据源返回的当前 ST 名单为空，但已存储的 st_history 有未结束的区间；拒绝全部关闭"
        )
    rows: list[tuple[str, Any, Any]] = []
    open_now: set[str] = set()
    for symbol, start, end in zip(stored["symbol"], stored["start"], stored["end"], strict=True):
        sym = str(symbol)
        if pd.isna(end) and sym in now_st:
            rows.append((sym, start, pd.NaT))
            open_now.add(sym)
        elif pd.isna(end):
            rows.append((sym, start, stamp))
        else:
            rows.append((sym, start, end))
    rows.extend((sym, stamp, pd.NaT) for sym in sorted(now_st - open_now))
    return pd.DataFrame(
        {
            "symbol": pd.Series([r[0] for r in rows], dtype="str"),
            "start": pd.Series([r[1] for r in rows], dtype="datetime64[ns]"),
            "end": pd.Series([r[2] for r in rows], dtype="datetime64[ns]"),
        }
    )


def merge_securities(source: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame:
    """数据源本次的证券表并上已存储、但本次数据源里没有的证券（保留其上次的行）。

    已存储的证券绝不因一次更新而消失：数据源偶尔返回空表或截断的表，或某只股票从列表里消失，
    都不能让它的历史从回测里消失。
    """
    missing = stored[~stored["symbol"].isin(source["symbol"])]
    if len(missing) == 0:
        return source.reset_index(drop=True)
    return pd.concat([source, missing], ignore_index=True)


def _shrink_problem(source: pd.DataFrame, stored: pd.DataFrame, limit: float) -> list[str]:
    """数据源本次的证券数比已存储的少超过 limit 比例时返回问题描述。"""
    have, now = len(stored), len(source)
    if have == 0 or now >= (1 - limit) * have:
        return []
    return [
        f"数据源本次返回的证券 {now} 只，比已存储的 {have} 只少 {1 - now / have:.1%}，"
        f"超过 {limit:.0%}；缺失的证券已按上次的记录保留，请检查数据源"
    ]


def _refresh_shared(
    source: DataSource,
    store: ParquetStore,
    cfg: AppConfig,
    as_of: date,
    refresh_trade_days: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """每次运行刷新一次的共享表；任何一张失败都抛 SharedTableError。

    返回 (数据源本次给出的 securities, 待定稿的合并表, 数据问题)。数据源本次的表已按配置板块过滤、
    退市日期未定稿，下载范围由它决定，而不是由磁盘上的表决定；合并表另含已存储而本次数据源
    没有的证券（见 merge_securities）。写盘的 securities 先按已存储的日线定稿退市日期：磁盘上的表
    任何时候都不含未定稿的日期或占位值；退市日期缺失且暂无日线的证券暂不写入（本次运行可能补到日线）。
    refresh_trade_days 为假时不再请求交易日历（调用方刚刷新过）。
    """
    found: list[tuple[pd.DataFrame, pd.DataFrame, list[str]]] = []
    if refresh_trade_days:
        _shared("trade_days", lambda: store.write(Table.TRADE_DAYS, source.trade_days()))

    def write_securities() -> None:
        secs = source.securities()
        in_boards = secs[secs["board"].isin(cfg.universe.boards)].reset_index(drop=True)
        stored = store.read(Table.SECURITIES)
        merged = merge_securities(in_boards, stored)
        stored_in_boards = stored[stored["board"].isin(cfg.universe.boards)]
        limit = cfg.data.max_securities_shrink
        problems = _shrink_problem(in_boards, stored_in_boards, limit)
        found.append((in_boards, merged, problems))
        store.write(Table.SECURITIES, finalize_securities(merged, store)[0])

    _shared("securities", write_securities)

    def write_st() -> None:
        merged = merge_st_history(store.read(Table.ST_HISTORY), source.st_history(), as_of)
        store.write(Table.ST_HISTORY, merged)

    _shared("st_history", write_st)

    def write_benchmarks() -> None:
        frames = [
            source.benchmark(code, cfg.backtest.data_start, as_of)
            for code in cfg.backtest.benchmarks
        ]
        merged = pd.concat(frames, ignore_index=True)
        merged = merged[merged["day"] <= pd.Timestamp(as_of)]  # 不依赖数据源遵守 end
        days = store.read(Table.TRADE_DAYS)
        if len(days) > 0:  # 中证指数含非交易日的行（如 2009-01-01），按交易日历过滤
            merged = merged[merged["day"].isin(days["day"])].reset_index(drop=True)
        store.write(Table.BENCHMARKS, merged)

    _shared("benchmarks", write_benchmarks)
    return found[0]


def _record(failed: dict[str, str], symbol: str, message: str) -> None:
    failed[symbol] = f"{failed[symbol]}；{message}" if symbol in failed else message


def _refresh_industries(
    source: DataSource,
    store: ParquetStore,
    cfg: AppConfig,
    targets: Sequence[str],
    as_of: date,
    failed: dict[str, str],
    progress: ProgressFn | None,
    progress_every: int,
    full_run: bool,
) -> list[str]:
    """增量更新行业分类，返回需要报告的数据问题。

    只查询 targets（在市股票）里表中缺失的股票。全量运行（full_run）时，表文件超过
    industries_refresh_days 天则全部重查；部分运行（--symbols）不做过期重查，
    且写回后恢复文件原有的修改时间，不重置没有被重新查询的股票的过期时钟。
    单只查询失败记入 failed，不中断。AKShare 对部分股票用报错而不是空表表示“没有分类”，
    因此真正没有分类的在市股票会记为“行业查询失败”，并在每次运行时重新查询。
    不支持逐只查询的数据源退回整表查询。
    """
    stored = store.read(Table.INDUSTRIES)
    if isinstance(source, IndustryLookup):
        stamp = store.modified_time(Table.INDUSTRIES)
        stale = (
            full_run
            and stamp is not None
            and (as_of - stamp.date()).days > cfg.data.industries_refresh_days
        )
        have = set(stored["symbol"])
        todo = list(targets) if stale else [s for s in targets if s not in have]
        found: dict[str, tuple[str, bool]] = {}
        guard = _OutageGuard(cfg.data.max_consecutive_failures, "行业查询")
        for i, symbol in enumerate(todo, start=1):
            try:
                classified = source.industry(symbol)
            except Exception as exc:  # 单只失败不影响其他股票
                _record(failed, symbol, f"{INDUSTRY_FAILED_PREFIX}：{type(exc).__name__}: {exc}")
                guard.failed(exc)  # 连续网络错误则中止，已查到的行业不写盘，下次重查
            else:
                guard.ok()
                if classified is not None:
                    found[symbol] = classified
            if progress is not None and i % progress_every == 0:
                progress("行业", i, len(todo))
        merged = {
            str(s): (str(n), bool(f))
            for s, n, f in zip(
                stored["symbol"], stored["industry"], stored["is_financial"], strict=True
            )
        }
        merged.update(found)
        if found or not store.has(Table.INDUSTRIES):
            store.write(Table.INDUSTRIES, build_industries(merged))
            if stamp is not None and not full_run:
                store.set_modified_time(Table.INDUSTRIES, None, stamp)
        have = set(merged)
    else:
        full = _shared("industries", source.industries)
        store.write(Table.INDUSTRIES, full)
        have = set(full["symbol"])

    covered = sum(1 for s in targets if s in have)
    if targets and covered < cfg.data.min_industry_coverage * len(targets):
        return [
            f"行业覆盖率 {covered}/{len(targets)}（{covered / len(targets):.0%}）"
            f"低于 {cfg.data.min_industry_coverage:.0%}"
        ]
    return []


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
    as_of: date,
    latest: date,
) -> bool:
    """更新一只股票的日线（不存 as_of 之后的行）；有新行写入返回 True。"""
    first_start = _years_before(cfg.backtest.in_sample_start, _LOOKBACK_YEARS)
    last = store.last_day(Table.BARS, symbol)
    if delisted and last is not None and (as_of - last).days > _LONG_DEAD_DAYS:
        return False  # 早已退市，不会再有新日线
    if last is not None and last >= latest:
        return False
    start = first_start if last is None else last + timedelta(days=1)
    df = source.bars(symbol, start, as_of)
    df = df[df["day"] <= pd.Timestamp(as_of)]
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


def _finalize_and_write(store: ParquetStore, merged: pd.DataFrame) -> list[str]:
    """按现已存储的日线定稿 merged 并写回 securities，返回被丢弃的代码。"""
    final, dropped = finalize_securities(merged, store)
    store.write(Table.SECURITIES, final)
    return dropped


def update_all(
    source: DataSource,
    store: ParquetStore,
    cfg: AppConfig,
    as_of: date,
    symbols: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    refresh_trade_days: bool = True,
) -> UpdateReport:
    """更新全部数据。

    as_of 是“最后一个已完整收盘的交易日”（见 latest_closed_day）：任何晚于 as_of 的日线都不入库。
    先刷新共享表（失败抛 SharedTableError；refresh_trade_days 为假时跳过交易日历，供刚刷新过它的
    调用方使用），再增量更新行业，最后逐只更新日线、分红、财务。
    symbols 非空时，逐股数据和行业查询都只处理这些股票；共享表仍整表刷新。
    单只股票失败记入 failed，不影响其他股票；但连续 data.max_consecutive_failures 只股票因网络错误
    失败时抛 NetworkOutageError 中止（已退市且取不到日线的不算）。运行结束时（包括被中断或中止）
    统一定稿全部退市证券的 delist_date；定稿本身出错时记日志，不掩盖原来的异常。
    progress(阶段, 已处理数, 总数) 每处理 progress_every 只调用一次。
    """
    if isinstance(source, AsOfAware):
        source.set_as_of(as_of)
    source_secs, merged, problems = _refresh_shared(source, store, cfg, as_of, refresh_trade_days)
    in_universe = source_secs.set_index("symbol")
    wanted = list(in_universe.index) if symbols is None else list(dict.fromkeys(symbols))

    failed: dict[str, str] = {}
    is_listed = in_universe["delist_date"].isna().to_dict()
    listed_wanted = [s for s in wanted if is_listed.get(s, False)]
    calendar_days = store.read(Table.TRADE_DAYS)
    latest = as_of
    if len(calendar_days) > 0:
        latest = latest_trading_day(load_calendar(store), as_of)
    first_start = _years_before(cfg.backtest.in_sample_start, _LOOKBACK_YEARS)
    window_start = pd.Timestamp(first_start) - pd.Timedelta(days=_DELIST_MARGIN_DAYS)

    updated = skipped = 0
    guard = _OutageGuard(cfg.data.max_consecutive_failures, "日线")
    try:
        problems = problems + _refresh_industries(
            source,
            store,
            cfg,
            listed_wanted,
            as_of,
            failed,
            progress,
            progress_every,
            symbols is None,
        )
        for i, symbol in enumerate(wanted, start=1):
            try:
                if symbol not in in_universe.index:
                    raise ValueError("不在配置板块的证券列表中")
                delist = cast(pd.Timestamp, in_universe.loc[symbol, "delist_date"])
                delisted = not pd.isna(delist)
                if delisted and delist < window_start:
                    skipped += 1  # 退市早于下载起点，窗口内不可能有日线
                else:
                    changed = _update_bars(source, store, cfg, symbol, delisted, as_of, latest)
                    _update_fundamentals(source, store, cfg, symbol, as_of)
                    updated, skipped = (updated + 1, skipped) if changed else (updated, skipped + 1)
                guard.ok()
            except _NoBarsError:
                _record(failed, symbol, f"{NO_BARS_PREFIX}：已退市股票取不到日线")
                guard.ok()
            except Exception as exc:  # 单只股票失败不影响其他股票
                _record(failed, symbol, f"{type(exc).__name__}: {exc}")
                guard.failed(exc)
            if progress is not None and i % progress_every == 0:
                progress("日线", i, len(wanted))
    except BaseException:
        # 被中断或中止：仍按现已存储的日线定稿 securities；定稿出错只记日志，抛出原来的异常
        try:
            _finalize_and_write(store, merged)
        except Exception:
            _log.exception("中断后定稿 securities 失败（原异常将继续抛出）")
        raise
    dropped = _finalize_and_write(store, merged)

    attempted = set(wanted)
    for symbol in dropped:
        if symbol in attempted:  # 本次没有尝试下载的不记失败
            message = f"{DELIST_MISSING_PREFIX}：退市表没有退市日期且无日线，未写入 securities"
            failed[symbol] = f"{message}；{failed[symbol]}" if symbol in failed else message
    return UpdateReport(updated=updated, skipped=skipped, failed=failed, problems=problems)


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


def check_integrity(
    store: ParquetStore,
    calendar: TradingCalendar,
    day: date,
    symbols: Sequence[str] | None = None,
) -> list[str]:
    """检查 day 当天的数据完整性，返回问题描述列表，空列表表示正常。

    day 不是交易日则不检查。symbols 非空时只检查这些股票。
    1. 覆盖率：day 当天在市（上市日 <= day 且未退市）的股票里，有日线的少于 80%；
    2. 跳变：day 有日线的股票，复权后（close * adj_factor）收盘价相对前一根日线的涨跌幅
       绝对值超过 21%。前一根日线必须正好在前一个交易日（停牌复牌后不查）；
       上市后前 5 个交易日不查。除权除息日的未复权价格跳变因此不会误报。
    """
    secs = store.read(Table.SECURITIES)
    if len(secs) == 0:
        return ["securities 表为空，无法检查"]
    if not calendar.is_trading_day(day):
        return []
    stamp = pd.Timestamp(day)
    listed = secs[
        (secs["list_date"] <= stamp) & (secs["delist_date"].isna() | (secs["delist_date"] > stamp))
    ]
    if symbols is not None:
        listed = listed[listed["symbol"].isin(list(symbols))]
    try:
        previous: date | None = calendar.offset(day, -1)
    except ValueError:
        previous = None
    bars = store.read(
        Table.BARS,
        symbols=None if symbols is None else list(symbols),
        start=previous or day,
        end=day,
    )
    today_bars = bars[bars["day"] == stamp].set_index("symbol")
    covered = [s for s in listed["symbol"] if s in today_bars.index]
    problems: list[str] = []
    if len(listed) > 0 and len(covered) < _MIN_COVERAGE * len(listed):
        problems.append(
            f"{day} 仅 {len(covered)}/{len(listed)} 只在市股票有日线，低于 {_MIN_COVERAGE:.0%}"
        )
    if previous is None:
        return problems
    prev_bars = bars[bars["day"] == pd.Timestamp(previous)].set_index("symbol")
    now_hfq = (today_bars["close"] * today_bars["adj_factor"]).to_dict()
    old_hfq = (prev_bars["close"] * prev_bars["adj_factor"]).to_dict()
    list_dates = dict(zip(listed["symbol"], listed["list_date"], strict=True))
    for symbol in covered:
        if symbol not in old_hfq:
            continue  # 前一个交易日没有日线（停牌或新股）：不查
        exempt_until = _ipo_exempt_until(calendar, list_dates[symbol])
        if exempt_until is not None and day <= exempt_until:
            continue
        change = now_hfq[symbol] / old_hfq[symbol] - 1 if old_hfq[symbol] != 0 else float("inf")
        if abs(change) > _MAX_JUMP:
            problems.append(f"{symbol} 在 {day} 复权收盘价单日变动 {change:+.1%}")
    return problems
