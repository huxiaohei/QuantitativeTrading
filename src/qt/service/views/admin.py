"""管理员页面的展示数据：邀请码、使用者列表。

只有账号层面的信息（邮箱、管理员写的备注、状态、注册时间、是否验证、订阅状态、邀请码状态），
绝不包含任何使用者的账本、现金、持仓、信号或日志内容（设计 6.7）。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from qt.db import admin_repo
from qt.db.models import Invite
from qt.service import auth
from qt.service.views.base import SUB_STATUS_LABELS

INVITE_STATE_LABELS = {"usable": "可用", "used": "已使用", "expired": "已过期", "revoked": "已作废"}
USER_STATUS_LABELS = {"active": "正常", "disabled": "已停用"}
NO_SUBSCRIPTION = "没有订阅"


@dataclass(frozen=True)
class InviteRow:
    """一条邀请码。code 只有在可用时才有值（页面据此给出注册链接）。"""

    id: int
    note: str
    state: str
    state_label: str
    created_at: datetime
    expires_at: datetime
    code: str | None


@dataclass(frozen=True)
class UserRow:
    """使用者列表的一行：只含账号信息。note 是管理员写的备注。"""

    id: int
    email: str
    note: str
    is_admin: bool
    status: str
    status_label: str
    created_at: datetime
    verified: bool
    subscription_label: str


def invite_row(invite: Invite, now: datetime) -> InviteRow:
    state = auth.invite_state(invite, now)
    return InviteRow(
        id=invite.id,
        note=invite.note,
        state=state,
        state_label=INVITE_STATE_LABELS[state],
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        code=invite.code if state == "usable" else None,
    )


def invite_rows(s: Session, now: datetime) -> list[InviteRow]:
    """全部邀请码，编号从新到旧。"""
    return [invite_row(invite, now) for invite in admin_repo.list_invites(s)]


def user_rows(s: Session) -> list[UserRow]:
    """全部使用者（按编号）及其订阅状态。"""
    subscription = admin_repo.subscription_status_by_user(s)
    return [
        UserRow(
            id=user.id,
            email=user.email,
            note=user.note,
            is_admin=user.is_admin,
            status=user.status,
            status_label=USER_STATUS_LABELS.get(user.status, user.status),
            created_at=user.created_at,
            verified=user.email_verified_at is not None,
            subscription_label=SUB_STATUS_LABELS.get(
                subscription.get(user.id, ""), NO_SUBSCRIPTION
            ),
        )
        for user in admin_repo.list_users(s)
    ]
