"""发件箱：入队去重与逐封发送。"""

import contextlib
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.db.models import Outbox
from qt.mail.outbox import enqueue, send_pending


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


def test_send_pending_marks_sent_and_retries_failures_up_to_limit(db: Session) -> None:
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


def test_send_pending_marks_failed_after_max_attempts(db: Session) -> None:
    _put(db, "k1")
    db.commit()
    sender = FlakySender(fail_times=100)
    for _ in range(4):
        assert send_pending(db, sender) == (0, 1)
        assert _row(db, "k1").status == "pending"
    assert send_pending(db, sender) == (0, 1)
    row = _row(db, "k1")
    assert (row.attempts, row.status) == (5, "failed")
    calls = sender.calls
    assert send_pending(db, sender) == (0, 0)
    assert sender.calls == calls


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
