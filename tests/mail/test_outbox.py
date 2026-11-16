"""发件箱：入队去重与逐封发送。"""

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from qt.db.models import Outbox
from qt.mail.account import ACCOUNT_KINDS
from qt.mail.outbox import enqueue, requeue_failed, send_one, send_pending
from qt.service.errors import NotFoundError, ValidationError


class FlakySender:
    """前 n 次发送抛异常，之后成功；记录成功发出的收件人。"""

    def __init__(self, fail_times: int, error: Callable[[], Exception] | None = None) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.delivered: list[str] = []
        self._error = error or (lambda: ConnectionError("连接被拒绝"))

    def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._error()
        self.delivered.append(subject)


def _count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Outbox)) or 0


def _row(db: Session, key: str) -> Outbox:
    return db.scalars(select(Outbox).where(Outbox.dedupe_key == key)).one()


def _put(db: Session, key: str, subject: str = "s") -> None:
    assert enqueue(db, None, "a@x.com", "buy_signal", key, subject, "h", "t") is True


def test_enqueue_same_dedupe_key_only_once(db: Session) -> None:
    assert enqueue(db, None, "a@x.com", "buy_signal", "buy:1:2026-11-16", "s", "h", "t") is True
    assert enqueue(db, None, "a@x.com", "buy_signal", "buy:1:2026-11-16", "s2", "h2", "t2") is False
    assert _count(db) == 1
    assert _row(db, "buy:1:2026-11-16").subject == "s"


def test_enqueue_duplicate_of_committed_row_keeps_session_usable(db: Session) -> None:
    _put(db, "k1")
    db.commit()
    assert enqueue(db, None, "a@x.com", "buy_signal", "k1", "s2", "h", "t") is False
    # 会话没有被毒化：之后还能继续写入和查询
    _put(db, "k2")
    assert _count(db) == 2


def test_enqueue_duplicate_does_not_roll_back_earlier_work(db: Session) -> None:
    _put(db, "k1")
    assert enqueue(db, None, "a@x.com", "buy_signal", "k1", "s2", "h", "t") is False
    db.commit()
    assert _count(db) == 1


def test_send_pending_marks_sent_and_retries_failures(db: Session) -> None:
    _put(db, "k1")
    db.commit()
    sender = FlakySender(fail_times=2)

    assert send_pending(db, sender) == (0, 1)
    row = _row(db, "k1")
    assert (row.attempts, row.status) == (1, "pending")
    assert row.last_error is not None and "连接被拒绝" in row.last_error

    assert send_pending(db, sender) == (0, 1)
    assert (_row(db, "k1").attempts, _row(db, "k1").status) == (2, "pending")

    assert send_pending(db, sender) == (1, 0)
    row = _row(db, "k1")
    assert (row.attempts, row.status) == (3, "sent")
    assert row.sent_at is not None and row.sent_at.tzinfo is not None


def test_send_pending_keeps_retrying_for_48_hours_then_marks_failed(db: Session) -> None:
    _put(db, "k1")
    db.commit()
    created = _row(db, "k1").created_at
    sender = FlakySender(fail_times=100)
    for hours in range(0, 48, 4):  # 两天里每 4 小时一次，次数远超过去的 5 次上限
        assert send_pending(db, sender, now=created + timedelta(hours=hours)) == (0, 1)
        assert _row(db, "k1").status == "pending"
    assert send_pending(db, sender, now=created + timedelta(hours=48, minutes=1)) == (0, 1)
    row = _row(db, "k1")
    assert (row.attempts, row.status) == (13, "failed")
    calls = sender.calls
    assert send_pending(db, sender, now=created + timedelta(hours=50)) == (0, 0)
    assert sender.calls == calls


def test_requeue_failed_mail_resets_age_clock(db: Session) -> None:
    _put(db, "k1")
    _put(db, "k2")
    db.commit()
    created = _row(db, "k1").created_at
    late = created + timedelta(hours=49)
    send_pending(db, FlakySender(fail_times=100), now=late)
    assert {_row(db, k).status for k in ("k1", "k2")} == {"failed"}
    [again] = requeue_failed(db, _row(db, "k1").id, late)
    db.commit()
    assert (again.status, again.attempts) == ("pending", 1)
    assert again.created_at == late and again.last_error is not None
    assert "重新排队" in again.last_error and "连接被拒绝" in again.last_error
    assert send_pending(db, FlakySender(fail_times=0), now=late + timedelta(hours=1)) == (1, 0)
    assert _row(db, "k1").status == "sent" and _row(db, "k2").status == "failed"
    with pytest.raises(ValidationError, match="发送失败"):
        requeue_failed(db, _row(db, "k1").id, late)  # 已发出的不能重新排队
    with pytest.raises(NotFoundError):
        requeue_failed(db, 999_999, late)
    assert [r.dedupe_key for r in requeue_failed(db, None, late)] == ["k2"]


def test_failed_admin_alert_is_logged_at_error_with_title(
    db: Session, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "s3cr3t-pw")
    assert enqueue(
        db, None, "admin@x.com", "admin", "admin:1", "【系统异常】数据更新失败", "h", "t"
    )
    _put(db, "k1", subject="给使用者的信号")
    db.commit()

    def error() -> Exception:
        return RuntimeError("login failed s3cr3t-pw")

    with caplog.at_level(logging.ERROR, logger="qt.mail.outbox"):
        send_pending(db, FlakySender(fail_times=100, error=error))
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "【系统异常】数据更新失败" in errors[0]
    assert "s3cr3t-pw" not in errors[0]


def test_send_pending_does_not_resend_sent_mail(db: Session) -> None:
    _put(db, "k1")
    db.commit()
    sender = FlakySender(fail_times=0)
    assert send_pending(db, sender) == (1, 0)
    assert send_pending(db, sender) == (0, 0)
    assert sender.calls == 1


def test_send_pending_commits_each_message_so_sent_stays_sent(db: Session) -> None:
    for key in ("k1", "k2", "k3"):
        _put(db, key, subject=key)
    db.commit()

    class CrashingSender:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
            if subject == "k3":
                raise KeyboardInterrupt  # 模拟进程在第三封时被打断
            self.delivered.append(subject)

    sender = CrashingSender()
    with contextlib.suppress(KeyboardInterrupt):
        send_pending(db, sender)
    db.rollback()
    assert _row(db, "k1").status == "sent"
    assert _row(db, "k2").status == "sent"
    assert _row(db, "k3").status == "pending"
    # 下一次运行只补发第三封
    again = FlakySender(fail_times=0)
    assert send_pending(db, again) == (1, 0)
    assert again.delivered == ["k3"]


def test_send_pending_error_text_does_not_leak_password(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "s3cr3t-pw")
    _put(db, "k1")
    db.commit()
    sender = FlakySender(fail_times=1, error=lambda: RuntimeError("login failed s3cr3t-pw"))
    send_pending(db, sender)
    err = _row(db, "k1").last_error
    assert err is not None and "login failed" in err and "s3cr3t-pw" not in err


def test_empty_smtp_password_does_not_mangle_error(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "")
    _put(db, "k1")
    db.commit()
    send_pending(db, FlakySender(fail_times=1, error=lambda: RuntimeError("boom")))
    assert _row(db, "k1").last_error == "RuntimeError: boom"


def _put_kind(db: Session, key: str, kind: str) -> int:
    assert enqueue(db, None, "a@x.com", kind, key, key, "h", "t") is True
    return _row(db, key).id


_NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


def test_send_one_success_marks_sent(db: Session) -> None:
    _put(db, "k1")
    oid = _row(db, "k1").id
    db.commit()
    sender = FlakySender(fail_times=0)
    assert send_one(db, sender, oid, _NOW) is True
    row = _row(db, "k1")
    assert (row.status, row.attempts, row.sent_at, row.last_error) == ("sent", 1, _NOW, None)
    assert sender.delivered == ["s"]


def test_send_one_failure_stays_pending(db: Session) -> None:
    _put(db, "k1")
    oid = _row(db, "k1").id
    db.commit()
    assert send_one(db, FlakySender(fail_times=1), oid, _NOW) is False
    row = _row(db, "k1")
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.last_error is not None and "连接被拒绝" in row.last_error


def test_send_one_on_sent_row_returns_false_without_sending(db: Session) -> None:
    _put(db, "k1")
    oid = _row(db, "k1").id
    db.commit()
    sender = FlakySender(fail_times=0)
    assert send_one(db, sender, oid, _NOW) is True
    assert send_one(db, sender, oid, _NOW) is False
    assert sender.calls == 1


def test_send_one_unknown_id_returns_false(db: Session) -> None:
    sender = FlakySender(fail_times=0)
    assert send_one(db, sender, 999999, _NOW) is False
    assert sender.calls == 0


def test_send_pending_with_kinds_only_touches_those_kinds(db: Session) -> None:
    _put_kind(db, "sig", "buy_signal")
    _put_kind(db, "acc", "account_verify")
    db.commit()
    sender = FlakySender(fail_times=0)
    assert send_pending(db, sender, kinds=ACCOUNT_KINDS) == (1, 0)
    assert sender.delivered == ["acc"]
    assert _row(db, "sig").status == "pending"
    assert _row(db, "acc").status == "sent"


def test_send_pending_skips_row_locked_by_another_session(db: Session, db_engine: Engine) -> None:
    oid = _put_kind(db, "acc", "account_verify")
    db.commit()
    other = Session(db_engine)
    try:
        # 另一个会话（模拟网页正在立即发送）锁住这一行
        other.scalars(select(Outbox).where(Outbox.id == oid).with_for_update()).one()
        sender = FlakySender(fail_times=0)
        assert send_pending(db, sender, kinds=ACCOUNT_KINDS) == (0, 0)
        assert send_one(db, sender, oid, _NOW) is False
        assert sender.calls == 0
    finally:
        other.rollback()
        other.close()
    assert send_pending(db, FlakySender(fail_times=0), kinds=ACCOUNT_KINDS) == (1, 0)
