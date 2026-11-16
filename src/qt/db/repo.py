"""业务查询：每个函数第一个参数是会话 s，第二个参数是 user_id。

记录不存在或属于其他使用者时一律抛 NotFoundError，不泄露他人数据是否存在。
跨使用者的查询只放在 admin_repo。
"""

from collections.abc import Collection, Mapping
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.db.models import (
    AuditLog,
    Book,
    BookDay,
    Followup,
    LedgerEntry,
    ParamChange,
    Rejection,
    RiskEvent,
    Signal,
    Subscription,
    SystemState,
    Trade,
    User,
)
from qt.service.actor import Actor
from qt.service.errors import NotFoundError


def get_user(s: Session, user_id: int) -> User:
    """按编号取使用者。"""
    user = s.get(User, user_id)
    if user is None:
        raise NotFoundError("使用者不存在")
    return user


def get_subscription(s: Session, user_id: int, subscription_id: int) -> Subscription:
    """取该使用者名下的订阅。"""
    sub = s.get(Subscription, subscription_id)
    if sub is None or sub.user_id != user_id:
        raise NotFoundError("订阅不存在")
    return sub


def get_open_subscription(s: Session, user_id: int) -> Subscription | None:
    """该使用者未关闭（生效或暂停）的订阅；没有则返回 None。"""
    stmt = select(Subscription).where(
        Subscription.user_id == user_id, Subscription.status != "closed"
    )
    return s.scalars(stmt).first()


def get_latest_subscription(s: Session, user_id: int) -> Subscription | None:
    """该使用者最近创建的订阅（含已关闭的）；一个都没有则返回 None。"""
    stmt = select(Subscription).where(Subscription.user_id == user_id)
    return s.scalars(stmt.order_by(Subscription.id.desc())).first()


def list_subscriptions(s: Session, user_id: int) -> list[Subscription]:
    """该使用者的全部订阅（含已关闭的），按创建时间倒序。"""
    stmt = select(Subscription).where(Subscription.user_id == user_id)
    return list(s.scalars(stmt.order_by(Subscription.created_at.desc(), Subscription.id.desc())))


def get_book(s: Session, user_id: int, book_id: int) -> Book:
    """取该使用者名下的账本。"""
    book = s.get(Book, book_id)
    if book is None or book.user_id != user_id:
        raise NotFoundError("账本不存在")
    return book


def list_books(s: Session, user_id: int, subscription_id: int) -> list[Book]:
    """订阅下的所有账本，按编号排序。"""
    get_subscription(s, user_id, subscription_id)
    stmt = select(Book).where(Book.user_id == user_id, Book.subscription_id == subscription_id)
    return list(s.scalars(stmt.order_by(Book.id)))


def get_book_of_kind(
    s: Session, user_id: int, subscription_id: int, kind: str, for_update: bool = False
) -> Book:
    """订阅下指定种类（system 或 manual）的账本。

    for_update 为真时对账本行加行锁（SELECT … FOR UPDATE，到事务结束），并用数据库里的最新值刷新：
    同一本账的写操作（每晚推进与管理员录入）由此串行。
    """
    stmt = select(Book).where(
        Book.user_id == user_id, Book.subscription_id == subscription_id, Book.kind == kind
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    book = s.scalars(stmt).first()
    if book is None:
        raise NotFoundError("账本不存在")
    return book


def get_system_state(s: Session, user_id: int, book_id: int) -> SystemState | None:
    """系统账保存的引擎状态；还没推进过则返回 None。"""
    state = s.get(SystemState, book_id)
    if state is None or state.user_id != user_id:
        return None
    return state


def find_signal(
    s: Session, user_id: int, book_id: int, side: str, symbol: str, signal_day: date
) -> Signal | None:
    """按自然键（账本、方向、股票、信号日）找信号；没有则返回 None。"""
    stmt = select(Signal).where(
        Signal.user_id == user_id,
        Signal.book_id == book_id,
        Signal.side == side,
        Signal.symbol == symbol,
        Signal.signal_day == signal_day,
    )
    return s.scalars(stmt).first()


def latest_rejection(
    s: Session, user_id: int, book_id: int, symbol: str, rule: str
) -> Rejection | None:
    """该账本中同一股票、同一规则最近的一条拒绝记录；没有则返回 None。"""
    stmt = select(Rejection).where(
        Rejection.user_id == user_id,
        Rejection.book_id == book_id,
        Rejection.symbol == symbol,
        Rejection.rule == rule,
    )
    return s.scalars(stmt.order_by(Rejection.last_day.desc(), Rejection.id.desc())).first()


def list_ledger_entries(s: Session, user_id: int, book_id: int) -> list[LedgerEntry]:
    """账本的全部流水（含作废记录），按编号升序（即录入顺序）。"""
    get_book(s, user_id, book_id)
    stmt = select(LedgerEntry).where(LedgerEntry.user_id == user_id, LedgerEntry.book_id == book_id)
    return list(s.scalars(stmt.order_by(LedgerEntry.id)))


def get_ledger_entry(s: Session, user_id: int, book_id: int, entry_id: int) -> LedgerEntry:
    """取该使用者某本账里的一条流水。"""
    entry = s.get(LedgerEntry, entry_id)
    if entry is None or entry.user_id != user_id or entry.book_id != book_id:
        raise NotFoundError("流水不存在")
    return entry


def list_signals(
    s: Session,
    user_id: int,
    book_id: int,
    side: str | None = None,
    statuses: Collection[str] | None = None,
) -> list[Signal]:
    """账本的信号，可按方向与状态过滤，按编号升序。"""
    get_book(s, user_id, book_id)
    stmt = select(Signal).where(Signal.user_id == user_id, Signal.book_id == book_id)
    if side is not None:
        stmt = stmt.where(Signal.side == side)
    if statuses is not None:
        stmt = stmt.where(Signal.status.in_(list(statuses)))
    return list(s.scalars(stmt.order_by(Signal.id)))


def list_risk_events(s: Session, user_id: int, book_id: int) -> list[RiskEvent]:
    """账本的风控事件，按日期、编号升序。"""
    get_book(s, user_id, book_id)
    stmt = select(RiskEvent).where(RiskEvent.user_id == user_id, RiskEvent.book_id == book_id)
    return list(s.scalars(stmt.order_by(RiskEvent.day, RiskEvent.id)))


def list_book_days(s: Session, user_id: int, book_id: int) -> list[BookDay]:
    """账本的每日净值，按日期升序。"""
    get_book(s, user_id, book_id)
    stmt = select(BookDay).where(BookDay.user_id == user_id, BookDay.book_id == book_id)
    return list(s.scalars(stmt.order_by(BookDay.day)))


def list_trades(s: Session, user_id: int, book_id: int) -> list[Trade]:
    """账本里已完成的交易，按卖出日、编号升序。"""
    get_book(s, user_id, book_id)
    stmt = select(Trade).where(Trade.user_id == user_id, Trade.book_id == book_id)
    return list(s.scalars(stmt.order_by(Trade.exit_day, Trade.id)))


def list_followups(s: Session, user_id: int, book_id: int) -> list[Followup]:
    """账本的跟踪记录，按事件日、编号升序。"""
    get_book(s, user_id, book_id)
    stmt = select(Followup).where(Followup.user_id == user_id, Followup.book_id == book_id)
    return list(s.scalars(stmt.order_by(Followup.event_day, Followup.id)))


def list_param_changes(s: Session, user_id: int, subscription_id: int) -> list[ParamChange]:
    """订阅的参数修改历史，按时间先后排序。"""
    get_subscription(s, user_id, subscription_id)
    stmt = select(ParamChange).where(
        ParamChange.user_id == user_id, ParamChange.subscription_id == subscription_id
    )
    return list(s.scalars(stmt.order_by(ParamChange.id)))


def add_audit(
    s: Session,
    user_id: int,
    action: str,
    target: str,
    detail: Mapping[str, Any],
    actor: Actor,
) -> AuditLog:
    """写一条审计日志：user_id 是被操作的使用者（记入 detail），actor 记录操作人与渠道。"""
    entry = AuditLog(
        actor_user_id=actor.user_id,
        channel=actor.channel,
        action=action,
        target=target,
        detail={**detail, "user_id": user_id},
    )
    s.add(entry)
    s.flush()
    return entry
