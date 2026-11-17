"""盘中报价（只读，只用于网页显示）：腾讯报价接口、进程内共用缓存、交易时段判断。

信号、止损止盈、净值都不使用这里的数据，它们只在每晚按收盘价计算。
"""

import logging
import threading
import urllib.request
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from qt.calendar import TradingCalendar

log = logging.getLogger(__name__)

_URL = "https://qt.gtimg.cn/q="
_TIMEOUT_SECONDS = 3.0
_BATCH = 60
_MAX_BYTES = 1_000_000
# 腾讯报价一行里用 ~ 分隔的字段位置
_F_CODE, _F_PRICE, _F_PREV_CLOSE, _F_TIME = 2, 3, 4, 30
_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
_LOG_EVERY = timedelta(minutes=1)
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
LIVE_SECONDS = 15
_IDLE_MAX_SECONDS = 600


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """报价接口不应当重定向：遇到重定向一律当作失败，不跟随到别的地址。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


_OPENER = urllib.request.build_opener(NoRedirect)


@dataclass(frozen=True)
class LiveQuote:
    """一只股票的盘中报价。price 为 0 表示停牌或当天还没有成交；at 是上海时间（无时区）。"""

    price: float
    prev_close: float
    at: datetime


QuoteFetcher = Callable[[Collection[str]], dict[str, LiveQuote]]


def exchange_prefix(symbol: str) -> str | None:
    """6 位代码所属交易所的前缀；认不出的返回 None。"""
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        return None
    head = symbol[0]
    if head in "69":
        return "sh"
    if head in "023":
        return "sz"
    if head in "48":
        return "bj"
    return None


def _parse_line(line: str) -> tuple[str, LiveQuote] | None:
    _, quoted, body = line.partition('="')
    if not quoted:
        return None
    fields = body.rstrip('";').split("~")
    if len(fields) <= _F_TIME:
        return None
    symbol = fields[_F_CODE]
    if exchange_prefix(symbol) is None:
        return None
    try:
        price = float(fields[_F_PRICE])
        prev_close = float(fields[_F_PREV_CLOSE])
        at = datetime.strptime(fields[_F_TIME], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    # nan、inf、负数都不是可用的价格
    if not (0 <= price < float("inf") and 0 <= prev_close < float("inf")):
        return None
    return symbol, LiveQuote(price=price, prev_close=prev_close, at=at)


def parse_tencent(text: str) -> dict[str, LiveQuote]:
    """解析腾讯报价的返回文本；解析不了的行丢弃。"""
    quotes: dict[str, LiveQuote] = {}
    for line in text.split(";"):
        parsed = _parse_line(line.strip())
        if parsed is not None:
            quotes[parsed[0]] = parsed[1]
    return quotes


def fetch_tencent(symbols: Collection[str]) -> dict[str, LiveQuote]:
    """向腾讯报价接口取一组股票的报价（超时 3 秒，不重试）。认不出交易所的代码跳过。"""
    codes = sorted(
        f"{prefix}{symbol}"
        for symbol in set(symbols)
        if (prefix := exchange_prefix(symbol)) is not None
    )
    quotes: dict[str, LiveQuote] = {}
    for start in range(0, len(codes), _BATCH):
        url = _URL + ",".join(codes[start : start + _BATCH])
        quotes.update(parse_tencent(read_url(url).decode("gbk", errors="replace")))
    return quotes


def read_url(url: str) -> bytes:
    with _OPENER.open(url, timeout=_TIMEOUT_SECONDS) as response:
        raw: bytes = response.read(_MAX_BYTES)
    return raw


def _market_time(now: datetime) -> datetime:
    """换成上海时间；不带时区的按上海时间看待。"""
    return now if now.tzinfo is None else now.astimezone(_MARKET_TZ)


def in_session(now: datetime, calendar: TradingCalendar) -> bool:
    """now 是否落在交易日的 09:30 到 11:30 或 13:00 到 15:00 之内（上海时间，含两端）。"""
    local = _market_time(now)
    if not calendar.is_trading_day(local.date()):
        return False
    clock = local.time().replace(microsecond=0)
    return any(start <= clock <= end for start, end in _SESSIONS)


@dataclass(frozen=True)
class LivePlan:
    """页面此刻该怎么做。use_quotes：持仓表是否用盘中报价（交易日开盘到收盘之间，含午间休市，
    午间显示的是上午最后的报价）；seconds：页面过多少秒再来取一次，None 表示今天不用再取。
    """

    use_quotes: bool
    seconds: int | None


def live_plan(now: datetime, calendar: TradingCalendar) -> LivePlan:
    """交易时段内每 LIVE_SECONDS 秒取一次；开盘前、午间休市时等到下一段开始再取（最长
    _IDLE_MAX_SECONDS 秒来一次，最短 LIVE_SECONDS 秒）；收盘后和非交易日不取。
    """
    local = _market_time(now)
    if not calendar.is_trading_day(local.date()):
        return LivePlan(False, None)
    clock = local.time().replace(microsecond=0)
    opened, closed = _SESSIONS[0][0], _SESSIONS[-1][1]
    if clock > closed:
        return LivePlan(False, None)
    use_quotes = clock >= opened
    if in_session(now, calendar):
        return LivePlan(use_quotes, LIVE_SECONDS)
    next_start = next(start for start, _ in _SESSIONS if start > clock)
    wait = datetime.combine(local.date(), next_start) - datetime.combine(local.date(), clock)
    seconds = min(max(int(wait.total_seconds()), LIVE_SECONDS), _IDLE_MAX_SECONDS)
    return LivePlan(use_quotes, seconds)


def same_day(quotes: dict[str, LiveQuote], now: datetime) -> dict[str, LiveQuote]:
    """只留报价时间是今天的：前一个交易日留在缓存里的旧报价、长期停牌股票的旧时间都不算盘中价。"""
    today = _market_time(now).date()
    return {symbol: quote for symbol, quote in quotes.items() if quote.at.date() == today}


class QuoteCache:
    """进程内的报价缓存，所有使用者共用。

    get 只去取已过期或没有的股票；取失败时保留旧值，取成功但来源没给的股票则去掉。
    同一时刻最多一个在途请求：拿不到锁的调用不等待，直接用现有缓存。
    """

    def __init__(
        self, fetch: QuoteFetcher, *, ttl_seconds: float = 10.0, max_size: int = 2000
    ) -> None:
        self._fetch = fetch
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_size = max_size
        self._quotes: dict[str, tuple[datetime, LiveQuote]] = {}
        self._fetching = threading.Lock()
        self._last_logged: datetime | None = None

    def get(self, symbols: Collection[str], now: datetime) -> dict[str, LiveQuote]:
        wanted = set(symbols)
        stale = {
            symbol
            for symbol in wanted
            if (hit := self._quotes.get(symbol)) is None or now - hit[0] >= self._ttl
        }
        if stale and self._fetching.acquire(blocking=False):
            try:
                self._refresh(stale, now)
            finally:
                self._fetching.release()
        current = dict(self._quotes)
        return {symbol: current[symbol][1] for symbol in wanted if symbol in current}

    def _refresh(self, stale: set[str], now: datetime) -> None:
        try:
            fresh = self._fetch(stale)
        except Exception as exc:  # 报价只用于显示：任何失败都退回旧值或收盘价，不让页面出错
            if self._last_logged is None or now - self._last_logged >= _LOG_EVERY:
                self._last_logged = now
                log.warning("取盘中报价失败（%s），页面暂用旧报价或收盘价", type(exc).__name__)
            return
        merged = dict(self._quotes)
        for symbol in stale:
            quote = fresh.get(symbol)
            if quote is None:
                # 这次取成功了但来源没有这只股票：不再沿用旧报价
                merged.pop(symbol, None)
            else:
                merged[symbol] = (now, quote)
        if len(merged) > self._max_size:
            newest = sorted(merged.items(), key=lambda item: item[1][0], reverse=True)
            merged = dict(newest[: self._max_size])
        self._quotes = merged
