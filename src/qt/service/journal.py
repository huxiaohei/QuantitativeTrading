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

# followups 表的固定列：跟踪天数 -> 列名
_COLUMNS = {20: "ret_20", 60: "ret_60"}


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


def open_followups(s: Session, book: Book, day: date, market: AdjMarket) -> int:
    """为这本账在 day 发生的事件各建一条跟踪记录，返回新建条数；重复调用不会重复建。

    事件：当日卖出的交易（exit）、当晚变为放弃的买入信号（abandoned）、当日首次被否决的候选
    （rejected）。事件日及之前没有任何复权收盘价的股票不建。只 flush，不提交。
    """
    events: list[tuple[str, str]] = []
    events += [
        ("exit", t.symbol)
        for t in s.scalars(
            select(Trade).where(
                Trade.book_id == book.id, Trade.user_id == book.user_id, Trade.exit_day == day
            )
        )
    ]
    events += [
        ("abandoned", g.symbol)
        for g in s.scalars(
            select(Signal).where(
                Signal.book_id == book.id,
                Signal.user_id == book.user_id,
                Signal.side == "buy",
                Signal.status == "abandoned",
                Signal.status_day == day,
            )
        )
    ]
    events += [
        ("rejected", r.symbol)
        for r in s.scalars(
            select(Rejection).where(
                Rejection.book_id == book.id,
                Rejection.user_id == book.user_id,
                Rejection.first_day == day,
            )
        )
    ]
    existing = {
        (f.event, f.symbol)
        for f in s.scalars(
            select(Followup).where(
                Followup.book_id == book.id,
                Followup.user_id == book.user_id,
                Followup.event_day == day,
            )
        )
    }
    created = 0
    for event, symbol in dict.fromkeys(events):  # 去重并保持顺序
        if (event, symbol) in existing:
            continue
        base = market.last_adj_close(day, symbol)
        if base is None or base <= 0:
            continue
        s.add(
            Followup(
                book_id=book.id,
                user_id=book.user_id,
                symbol=symbol,
                event=event,
                event_day=day,
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
        if h not in _COLUMNS:
            raise ValueError(f"跟踪天数只支持 {sorted(_COLUMNS)}，收到 {h}")
    filled = 0
    for f in admin_repo.list_unfilled_followups(s, day):
        for h in horizons:
            column = _COLUMNS[h]
            if getattr(f, column) is not None:
                continue
            try:
                due = calendar.offset(f.event_day, h)
            except ValueError:
                continue  # 日历还没排到那一天
            if due > day:
                continue
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
            setattr(f, column, price / f.base_adj_close - 1)
            filled += 1
    s.flush()
    return filled
