"""跨使用者查询：只给每日运行、管理后台等系统级调用方使用，业务接口不得引用。"""

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from qt.db.models import DailyRun, Followup, Invite, Outbox, Subscription, User


def find_user_by_email(s: Session, email: str) -> User | None:
    """按邮箱（不区分大小写）找使用者；没有则返回 None。"""
    return s.scalars(select(User).where(User.email == email.strip().lower())).first()


def list_users(s: Session) -> list[User]:
    """全部使用者，按编号排序。"""
    return list(s.scalars(select(User).order_by(User.id)))


def list_active_subscriptions(s: Session) -> list[Subscription]:
    """全部处于生效状态、且所属使用者未被停用的订阅（暂停与关闭的不含），按编号排序。"""
    stmt = (
        select(Subscription)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.status == "active", User.status == "active")
        .order_by(Subscription.id)
    )
    return list(s.scalars(stmt))


def list_unfilled_followups(s: Session, since: date, before: date) -> list[Followup]:
    """事件日在 [since, before) 内、20 日或 60 日收益还没填的跟踪记录（全部使用者），按编号排序。"""
    stmt = (
        select(Followup)
        .where(
            Followup.event_day >= since,
            Followup.event_day < before,
            or_(Followup.ret_20.is_(None), Followup.ret_60.is_(None)),
        )
        .order_by(Followup.id)
    )
    return list(s.scalars(stmt))


def list_recent_runs(s: Session, limit: int) -> list[DailyRun]:
    """最近 limit 个交易日的每日运行记录，日期从新到旧。"""
    stmt = select(DailyRun).order_by(DailyRun.trade_day.desc()).limit(limit)
    return list(s.scalars(stmt))


def list_outbox(s: Session, status: str | None, limit: int) -> list[Outbox]:
    """发件箱里最近的 limit 封邮件（全部使用者，可按状态过滤），编号从新到旧。"""
    stmt = select(Outbox)
    if status is not None:
        stmt = stmt.where(Outbox.status == status)
    return list(s.scalars(stmt.order_by(Outbox.id.desc()).limit(limit)))


def count_outbox_by_status(s: Session) -> dict[str, int]:
    """发件箱里各状态（pending、sent、failed）的邮件数；没有的状态不在其中。"""
    stmt = select(Outbox.status, func.count()).group_by(Outbox.status)
    return {str(row[0]): int(row[1]) for row in s.execute(stmt)}


def list_invites(s: Session) -> list[Invite]:
    """全部邀请码，编号从新到旧。"""
    return list(s.scalars(select(Invite).order_by(Invite.id.desc())))
