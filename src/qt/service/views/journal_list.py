"""交易日志的展示：已完成的交易与离场、放弃、被否决之后的跟踪记录。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from qt.db import repo
from qt.service.views.base import (
    EXIT_LABELS,
    FOLLOWUP_LABELS,
    BookKind,
    Names,
    SubscriptionView,
    name_of,
    origin_label,
    subscription_view,
)


@dataclass(frozen=True)
class TradeRow:
    """一笔已完成的交易。received 为卖出净额加持有期间收到的现金分红，ret = received / cost - 1。"""

    symbol: str
    name: str
    entry_day: date
    exit_day: date
    shares: float
    cost: float
    received: float
    ret: float
    holding_days: int
    exit_reason: str
    exit_label: str
    origin: str
    origin_label: str


@dataclass(frozen=True)
class FollowupRow:
    """一条跟踪记录：事件之后 20、60 个交易日的复权涨跌，还没到期为 None。"""

    symbol: str
    name: str
    event: str
    event_label: str
    event_day: date
    ret_20: float | None
    ret_60: float | None


@dataclass(frozen=True)
class BookJournal:
    """一本账的日志：交易按卖出日先后，跟踪记录按事件日先后。"""

    book: BookKind
    trades: list[TradeRow]
    followups: list[FollowupRow]


@dataclass(frozen=True)
class JournalView:
    """日志页：两本账各一份。"""

    subscription: SubscriptionView
    manual: BookJournal
    system: BookJournal


def book_journal(
    s: Session, user_id: int, subscription_id: int, book: BookKind, *, names: Names | None = None
) -> BookJournal:
    """一本账的已完成交易与跟踪记录。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    row = repo.get_book_of_kind(s, user_id, sub.id, book)
    trades = [
        TradeRow(
            symbol=t.symbol,
            name=name_of(names, t.symbol),
            entry_day=t.entry_day,
            exit_day=t.exit_day,
            shares=t.shares,
            cost=t.cost,
            received=t.proceeds + t.cash_received,
            ret=(t.proceeds + t.cash_received) / t.cost - 1,
            holding_days=t.holding_days,
            exit_reason=t.exit_reason,
            exit_label=EXIT_LABELS.get(t.exit_reason, t.exit_reason),
            origin=t.origin,
            origin_label=origin_label(t.origin),
        )
        for t in repo.list_trades(s, user_id, row.id)
    ]
    followups = [
        FollowupRow(
            symbol=f.symbol,
            name=name_of(names, f.symbol),
            event=f.event,
            event_label=FOLLOWUP_LABELS.get(f.event, f.event),
            event_day=f.event_day,
            ret_20=f.ret_20,
            ret_60=f.ret_60,
        )
        for f in repo.list_followups(s, user_id, row.id)
    ]
    return BookJournal(book, trades, followups)


def journal(
    s: Session, user_id: int, subscription_id: int, *, names: Names | None = None
) -> JournalView:
    """日志页：手动账与系统账的已完成交易、跟踪记录。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    return JournalView(
        subscription=subscription_view(sub),
        manual=book_journal(s, user_id, sub.id, "manual", names=names),
        system=book_journal(s, user_id, sub.id, "system", names=names),
    )
