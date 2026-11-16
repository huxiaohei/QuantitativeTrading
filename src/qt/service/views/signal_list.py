"""信号与被否决候选的展示：信号页、总览里今天的信号、命令行 qt admin signals 共用。"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.db import repo
from qt.db.models import Rejection, Signal, Subscription
from qt.service.errors import NotFoundError
from qt.service.manual_ops import buy_valid_on
from qt.service.signals import SignalStatus
from qt.service.views.base import (
    BOOKS,
    SIDE_LABELS,
    SIGNAL_STATUS_LABELS,
    BookKind,
    Names,
    SubscriptionView,
    name_of,
    subscription_view,
)

# 信号页历史信号、被否决候选各最多显示的条数（最近的在前）
HISTORY_LIMIT = 200
REJECTION_LIMIT = 100
_OPEN = frozenset({SignalStatus.CREATED.value, SignalStatus.NOTIFIED.value})


@dataclass(frozen=True)
class SignalRow:
    """一条信号（按你设定的规则计算出的提示）。

    can_record 为真时可以"按此信号录入"成交：手动账、未结、买入信号未过有效期、订阅未关闭。
    """

    id: int
    book: BookKind
    side: str  # buy | sell
    side_label: str
    symbol: str
    name: str
    signal_day: date
    valid_until: date | None
    shares: float
    ref_price: float
    status: str
    status_label: str
    status_day: date
    reason: str
    void_note: str | None
    can_record: bool


@dataclass(frozen=True)
class RejectionRow:
    """一个被规则否决的候选（同一只股票连续被否决算一次，first_day 到 last_day）。"""

    id: int
    book: BookKind
    symbol: str
    name: str
    rule: str
    message: str
    first_day: date
    last_day: date


@dataclass(frozen=True)
class SignalsView:
    """信号页：待处理的信号（手动账里未结的）、历史信号、被否决的候选，都是最近的在前。"""

    subscription: SubscriptionView
    pending: list[SignalRow]
    history: list[SignalRow]
    rejections: list[RejectionRow]


def _can_record(
    sig: Signal,
    book: BookKind,
    sub: Subscription,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
) -> bool:
    if book != "manual" or sub.status == "closed" or sig.status not in _OPEN:
        return False
    if sig.side == "buy":
        return buy_valid_on(sig, today, calendar, cfg.service.signal_valid_days)
    return True


def signal_row(
    sig: Signal,
    book: BookKind,
    sub: Subscription,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    names: Names | None,
) -> SignalRow:
    return SignalRow(
        id=sig.id,
        book=book,
        side=sig.side,
        side_label=SIDE_LABELS.get(sig.side, sig.side),
        symbol=sig.symbol,
        name=name_of(names, sig.symbol),
        signal_day=sig.signal_day,
        valid_until=sig.valid_until,
        shares=sig.shares,
        ref_price=sig.ref_price,
        status=sig.status,
        status_label=SIGNAL_STATUS_LABELS.get(sig.status, sig.status),
        status_day=sig.status_day,
        reason=sig.reason,
        void_note=sig.void_note,
        can_record=_can_record(sig, book, sub, calendar, cfg, today),
    )


def signal_rows(
    s: Session,
    user_id: int,
    subscription_id: int,
    book: BookKind,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    statuses: Collection[str] | None = None,
    since: date | None = None,
    names: Names | None = None,
) -> list[SignalRow]:
    """一本账的信号，编号从新到旧；可按状态过滤，since 给出时只要信号日不早于它的。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    row = repo.get_book_of_kind(s, user_id, sub.id, book)
    found = repo.list_signals(s, user_id, row.id, statuses=statuses)
    return [
        signal_row(sig, book, sub, calendar, cfg, today, names)
        for sig in reversed(found)
        if since is None or sig.signal_day >= since
    ]


def _rejection_row(rej: Rejection, book: BookKind, names: Names | None) -> RejectionRow:
    return RejectionRow(
        id=rej.id,
        book=book,
        symbol=rej.symbol,
        name=name_of(names, rej.symbol),
        rule=rej.rule,
        message=rej.message,
        first_day=rej.first_day,
        last_day=rej.last_day,
    )


def signals_page(
    s: Session,
    user_id: int,
    subscription_id: int,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    names: Names | None = None,
) -> SignalsView:
    """信号页：手动账里未结的信号在最上；两本账的其余信号按信号日从新到旧；被否决的候选。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    rows: list[SignalRow] = []
    rejections: list[RejectionRow] = []
    for book in BOOKS:
        rows += signal_rows(s, user_id, sub.id, book, calendar, cfg, today, names=names)
        row = repo.get_book_of_kind(s, user_id, sub.id, book)
        found = repo.list_rejections(s, user_id, row.id)
        rejections += [_rejection_row(r, book, names) for r in found]
    pending = [r for r in rows if r.book == "manual" and r.status in _OPEN]
    history = [r for r in rows if not (r.book == "manual" and r.status in _OPEN)]
    history.sort(key=lambda r: (r.signal_day, r.id), reverse=True)
    rejections.sort(key=lambda r: (r.last_day, r.id), reverse=True)
    return SignalsView(
        subscription=subscription_view(sub),
        pending=pending,
        history=history[:HISTORY_LIMIT],
        rejections=rejections[:REJECTION_LIMIT],
    )


def recordable_signal(
    s: Session,
    user_id: int,
    subscription_id: int,
    signal_id: int,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    names: Names | None = None,
) -> SignalRow:
    """本人手动账里可以"按此信号录入"的信号；别人的、不存在的、已结的、过期的都抛 NotFoundError。"""
    for row in signal_rows(
        s, user_id, subscription_id, "manual", calendar, cfg, today, names=names
    ):
        if row.id == signal_id and row.can_record:
            return row
    raise NotFoundError("信号不存在")
