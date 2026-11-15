"""交易日志的跟踪记录：离场、放弃、被否决之后，这只股票接下来 20 与 60 个交易日的走势。

收益一律用复权收盘价：到期价 / 事件日价 - 1。事件日停牌时，事件日价取此前最后一个复权收盘价。
"""

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.db import admin_repo
from qt.db.models import Book, Followup, Rejection, Signal, Trade

# 只回填最近这么多个交易日（跟踪天数最大值再加这个数）内的事件，更早还空着的不再查
_FILL_SLACK_DAYS = 30
_HORIZONS = (20, 60)  # followups 表只有 ret_20、ret_60 两列


class AdjMarket(Protocol):
    """跟踪记录用的复权行情。"""

    def adj_close(self, day: date, symbol: str) -> float | None:
        """day 当天的复权收盘价；当天没有数据为 None。"""
        ...

    def last_adj_close(self, day: date, symbol: str) -> float | None:
        """截至 day（含）最后一个复权收盘价；此前没有任何行情为 None。"""
        ...

    def last_adj_day(self, symbol: str) -> date | None:
        """存储中这只股票最后一个有复权收盘价的日期；完全没有行情为 None。"""
        ...


def _window_start(calendar: TradingCalendar, day: date, n: int) -> date:
    """截至 day（含）最近 n 个交易日的第一天；日历不够长时取日历第一天。"""
    if calendar.count_between(calendar.first_day, day) + 1 <= n:
        return calendar.first_day
    return calendar.offset(day, -(n - 1))


def open_followups(
    s: Session,
    book: Book,
    day: date,
    market: AdjMarket,
    calendar: TradingCalendar,
    lookback_days: int,
) -> int:
    """为这本账在回看窗口内发生、还没有记录的事件各建一条跟踪记录，返回新建条数。

    窗口是截至 day（含）最近 lookback_days 个交易日，补上晚录入、补录过去日期的卖出与跑漏的晚上。
    事件：卖出的交易（exit）、变为放弃的买入信号（abandoned）、首次被否决的候选（rejected），
    事件日取交易的卖出日、信号的状态日、否决的首日。基准价为事件日的最后一个复权收盘价，
    事件日及之前没有任何复权收盘价的不建。重复调用不会重复建。只 flush，不提交。
    """
    start = _window_start(calendar, day, lookback_days)
    events: list[tuple[str, str, date]] = []
    events += [
        ("exit", t.symbol, t.exit_day)
        for t in s.scalars(
            select(Trade).where(
                Trade.book_id == book.id,
                Trade.user_id == book.user_id,
                Trade.exit_day.between(start, day),
            )
        )
    ]
    events += [
        ("abandoned", g.symbol, g.status_day)
        for g in s.scalars(
            select(Signal).where(
                Signal.book_id == book.id,
                Signal.user_id == book.user_id,
                Signal.side == "buy",
                Signal.status == "abandoned",
                Signal.status_day.between(start, day),
            )
        )
    ]
    events += [
        ("rejected", r.symbol, r.first_day)
        for r in s.scalars(
            select(Rejection).where(
                Rejection.book_id == book.id,
                Rejection.user_id == book.user_id,
                Rejection.first_day.between(start, day),
            )
        )
    ]
    existing = {
        (f.event, f.symbol, f.event_day)
        for f in s.scalars(
            select(Followup).where(
                Followup.book_id == book.id,
                Followup.user_id == book.user_id,
                Followup.event_day.between(start, day),
            )
        )
    }
    created = 0
    for event, symbol, event_day in dict.fromkeys(events):  # 去重并保持顺序
        if (event, symbol, event_day) in existing:
            continue
        base = market.last_adj_close(event_day, symbol)
        if base is None or base <= 0:
            continue
        s.add(
            Followup(
                book_id=book.id,
                user_id=book.user_id,
                symbol=symbol,
                event=event,
                event_day=event_day,
                base_adj_close=base,
            )
        )
        created += 1
    s.flush()
    return created


def fill_followups(
    s: Session,
    calendar: TradingCalendar,
    market: AdjMarket,
    day: date,
    horizons: Sequence[int],
) -> int:
    """把已到期的跟踪收益填上（全部使用者），返回本次填上的格子数。

    事件日之后第 N 个交易日已到（不晚于 day）才填；到期日停牌则用其后第一个有价格的交易日，
    该日也还没到就等。已退市（存储里到期日及之后再没有价格）的格子留空。只 flush，不提交。
    """
    for h in horizons:
        if h not in _HORIZONS:
            raise ValueError(f"跟踪天数只支持 {list(_HORIZONS)}，收到 {h}")
    if not horizons:
        return 0
    since = _window_start(calendar, day, max(horizons) + _FILL_SLACK_DAYS)
    filled = 0
    for f in admin_repo.list_unfilled_followups(s, since, day):
        for h in horizons:
            if (f.ret_20 if h == 20 else f.ret_60) is not None:
                continue
            if calendar.count_between(f.event_day, day) < h:
                continue  # 第 h 个交易日还没到（日历也还没排到）
            due = calendar.offset(f.event_day, h)
            last = market.last_adj_day(f.symbol)
            if last is None or last < due:
                continue  # 已退市（或数据未到）：留空
            price = next(
                (
                    p
                    for d in calendar.between(due, day)
                    if (p := market.adj_close(d, f.symbol)) is not None
                ),
                None,
            )
            if price is None:
                continue
            ret = price / f.base_adj_close - 1
            if h == 20:
                f.ret_20 = ret
            else:
                f.ret_60 = ret
            filled += 1
    s.flush()
    return filled
