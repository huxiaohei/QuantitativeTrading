"""发件箱：邮件先入库（dedupe_key 去重），再逐封发送。"""

import os
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qt.db.models import Outbox
from qt.mail.send import Sender

_MAX_ERROR_CHARS = 500


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


def send_pending(s: Session, sender: Sender, max_attempts: int = 5) -> tuple[int, int]:
    """发送所有待发邮件，返回（成功数，失败数）。

    每封邮件单独加行锁、发送、提交：已标记为已发的邮件不会因后面的邮件出错而回滚。
    发送成功到提交之间进程崩溃，该封下次会重发一次（至少一次语义的唯一窗口）；
    提交之后绝不重发。会提交调用方会话里尚未提交的事务。失败次数达到上限后标记为 failed。
    """
    s.commit()
    ids = list(
        s.scalars(
            select(Outbox.id)
            .where(Outbox.status == "pending", Outbox.attempts < max_attempts)
            .order_by(Outbox.id)
        )
    )
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
            if row.attempts >= max_attempts:
                row.status = "failed"
            failed += 1
        else:
            row.attempts += 1
            row.status = "sent"
            row.sent_at = datetime.now(UTC)
            row.last_error = None
            sent += 1
        s.commit()
    return sent, failed
