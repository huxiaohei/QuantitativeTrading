"""业务查询：每个函数第一个参数是会话 s，第二个参数是 user_id。

记录不存在或属于其他使用者时一律抛 NotFoundError，不泄露他人数据是否存在。
跨使用者的查询只放在 admin_repo。
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.db.models import AuditLog, Book, ParamChange, Subscription, User
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


def list_param_changes(s: Session, user_id: int, subscription_id: int) -> list[ParamChange]:
    """订阅的参数修改历史，按时间先后排序。"""
    get_subscription(s, user_id, subscription_id)
    stmt = select(ParamChange).where(
        ParamChange.user_id == user_id, ParamChange.subscription_id == subscription_id
    )
    return list(s.scalars(stmt.order_by(ParamChange.id)))


def add_audit(
    s: Session, user_id: int, action: str, target: str, detail: Mapping[str, Any]
) -> AuditLog:
    """写一条审计日志，user_id 为操作者。"""
    entry = AuditLog(actor_user_id=user_id, action=action, target=target, detail=dict(detail))
    s.add(entry)
    s.flush()
    return entry
