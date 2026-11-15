"""跨使用者查询：只给每日运行、管理后台等系统级调用方使用，业务接口不得引用。"""

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from qt.db.models import Followup, Subscription, User


def find_user_by_email(s: Session, email: str) -> User | None:
    """按邮箱（不区分大小写）找使用者；没有则返回 None。"""
    return s.scalars(select(User).where(User.email == email.strip().lower())).first()


def list_users(s: Session) -> list[User]:
    """全部使用者，按编号排序。"""
    return list(s.scalars(select(User).order_by(User.id)))


def list_active_subscriptions(s: Session) -> list[Subscription]:
    """全部处于生效状态的订阅（暂停与关闭的不含），按编号排序。"""
    stmt = select(Subscription).where(Subscription.status == "active").order_by(Subscription.id)
    return list(s.scalars(stmt))


def list_unfilled_followups(s: Session, before: date) -> list[Followup]:
    """事件日早于 before、且 20 日或 60 日收益还没填的跟踪记录（全部使用者），按编号排序。"""
    stmt = (
        select(Followup)
        .where(
            Followup.event_day < before, or_(Followup.ret_20.is_(None), Followup.ret_60.is_(None))
        )
        .order_by(Followup.id)
    )
    return list(s.scalars(stmt))
