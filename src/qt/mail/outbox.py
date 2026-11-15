"""发件箱：邮件先入库（dedupe_key 去重），再逐封发送。

发送失败的邮件留在 pending，每次发送时重试，直到入队（或被管理员重新排队）满 48 小时仍未发出才标记为
failed；管理员可以用 qt admin outbox retry 把 failed 的邮件重新排队。
"""

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qt.db.models import Outbox
from qt.mail.send import Sender
from qt.service.errors import NotFoundError, ValidationError

_MAX_ERROR_CHARS = 500
MAX_PENDING_AGE = timedelta(hours=48)  # 待发邮件从入队（或重新排队）起最多重试这么久
_REQUEUE_MARK = "已于 {at} 重新排队；上次错误："

_log = logging.getLogger(__name__)


def enqueue(
    s: Session,
    user_id: int | None,
    to_addr: str,
    kind: str,
    dedupe_key: str,
    subject: str,
    body_html: str,
    body_text: str,
) -> bool:
    """入队一封邮件；dedupe_key 已存在（无论哪个事务写入）返回 False，不写入。

    用 ON CONFLICT DO NOTHING，冲突不会让事务进入失败状态；只执行语句，事务由调用方管理。
    """
    stmt = (
        insert(Outbox)
        .values(
            user_id=user_id,
            to_addr=to_addr,
            kind=kind,
            dedupe_key=dedupe_key,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
        )
        .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
        .returning(Outbox.id)
    )
    return s.execute(stmt).scalar_one_or_none() is not None


def _describe_error(exc: Exception) -> str:
    """错误文本：带类型、限长，并抹掉环境里的 SMTP 密码。"""
    text = f"{type(exc).__name__}: {exc}"
    password = os.environ.get("SMTP_PASSWORD", "")
    if password:  # 密码为空时 replace 会在每个字符间插入星号，必须跳过
        text = text.replace(password, "***")
    return text[:_MAX_ERROR_CHARS]


def send_pending(
    s: Session,
    sender: Sender,
    now: datetime | None = None,
    max_age: timedelta = MAX_PENDING_AGE,
) -> tuple[int, int]:
    """发送所有待发邮件，返回（成功数，失败次数）。

    每封邮件单独加行锁、发送、提交：已标记为已发的邮件不会因后面的邮件出错而回滚。
    发送成功到提交之间进程崩溃，该封下次会重发一次（至少一次语义的唯一窗口）；
    提交之后绝不重发。会提交调用方会话里尚未提交的事务。
    失败时记下次数与错误，邮件留在 pending 下次再试；created_at（入队或重新排队的时刻）距 now
    已满 max_age 时标记为 failed。给管理员的告警发送失败时另记一条 ERROR 日志（含标题，不含密码）。
    now 缺省为当前时间（每日任务传入它的时钟）。
    """
    moment = now if now is not None else datetime.now(UTC)
    s.commit()
    ids = list(s.scalars(select(Outbox.id).where(Outbox.status == "pending").order_by(Outbox.id)))
    s.commit()
    sent = failed = 0
    for outbox_id in ids:
        row = s.scalars(
            select(Outbox)
            .where(Outbox.id == outbox_id, Outbox.status == "pending")
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if row is None:  # 已被别的进程处理或正被处理
            s.commit()
            continue
        try:
            sender.send(row.to_addr, row.subject, row.body_html, row.body_text)
        except Exception as exc:
            row.attempts += 1
            row.last_error = _describe_error(exc)
            if moment - row.created_at >= max_age:
                row.status = "failed"
            if row.kind == "admin":
                _log.error("管理员告警邮件没有发出：%s（%s）", row.subject, row.last_error)
            failed += 1
        else:
            row.attempts += 1
            row.status = "sent"
            row.sent_at = moment
            row.last_error = None
            sent += 1
        s.commit()
    return sent, failed


def requeue_failed(s: Session, outbox_id: int | None, now: datetime) -> list[Outbox]:
    """把发送失败（failed）的邮件重新排队：outbox_id 为 None 时处理全部失败的邮件。

    状态改回 pending，created_at 改为 now（重新开始 48 小时的重试期），保留尝试次数，
    last_error 前面加上重新排队的时间。指定的邮件不存在抛 NotFoundError，不是 failed 抛
    ValidationError。只 flush、不 commit。
    """
    if outbox_id is None:
        rows: Sequence[Outbox] = list(
            s.scalars(select(Outbox).where(Outbox.status == "failed").order_by(Outbox.id))
        )
    else:
        row = s.get(Outbox, outbox_id)
        if row is None:
            raise NotFoundError(f"没有编号为 {outbox_id} 的邮件")
        if row.status != "failed":
            raise ValidationError(
                f"邮件 #{outbox_id} 的状态是 {row.status}，只有发送失败的邮件可以重新排队"
            )
        rows = [row]
    mark = _REQUEUE_MARK.format(at=now.isoformat(timespec="minutes"))
    for row in rows:
        row.status = "pending"
        row.created_at = now
        row.last_error = (mark + (row.last_error or ""))[:_MAX_ERROR_CHARS]
    s.flush()
    return list(rows)
