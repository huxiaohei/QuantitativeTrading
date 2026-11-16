"""账号类邮件：主题、链接、有效期、措辞与入队。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.db.models import Outbox, User
from qt.mail.account import (
    ACCOUNT_KINDS,
    queue_account_mail,
    render_password_changed,
    render_register_attempt,
    render_reset,
    render_verify,
)
from qt.mail.render import Mail

BASE = "https://qt.example.test"
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
TOKEN = "tok-AbC_123"


def _mails() -> list[tuple[str, Mail]]:
    return [
        ("verify", render_verify(BASE, TOKEN, 24)),
        ("reset", render_reset(BASE, TOKEN, 2)),
        ("attempt", render_register_attempt(BASE)),
        ("changed", render_password_changed(BASE, NOW, kept_current=False)),
    ]


def test_account_kinds_are_the_four_kinds() -> None:
    expected = {
        "account_verify",
        "account_reset",
        "account_register_attempt",
        "account_password_changed",
    }
    assert set(ACCOUNT_KINDS) == expected


def test_verify_and_reset_links_and_hours() -> None:
    verify = render_verify(BASE, TOKEN, 24)
    reset = render_reset(BASE, TOKEN, 2)
    for mail, path, hours in ((verify, "verify", "24"), (reset, "reset", "2")):
        link = f"{BASE}/{path}?token={TOKEN}"
        assert link in mail.html and link in mail.text
        assert f"{hours} 小时" in mail.html and f"{hours} 小时" in mail.text
    assert verify.subject != reset.subject


def test_base_url_trailing_slash_is_not_doubled() -> None:
    assert f"{BASE}/verify?token={TOKEN}" in render_verify(BASE + "/", TOKEN, 24).text


def test_token_is_url_escaped() -> None:
    assert "token=a%26b%3Dc" in render_verify(BASE, "a&b=c", 24).text


@pytest.mark.parametrize("name", ["verify", "reset", "attempt", "changed"])
def test_every_mail_has_text_ignore_notice_and_no_banned_words(name: str) -> None:
    mail = dict(_mails())[name]
    for body in (mail.subject, mail.html, mail.text):
        assert "建议" not in body and "推荐" not in body
    assert mail.text.strip() and "如果不是你本人操作" in mail.text and "忽略即可" in mail.text
    assert "忽略即可" in mail.html
    assert "<a href=" in mail.html and BASE in mail.html


def test_password_changed_shows_time_and_has_no_token_or_password() -> None:
    mail = render_password_changed(BASE, NOW, kept_current=False)
    assert "2026-11-16 09:30（北京时间）" in mail.text
    assert "2026-11-16 09:30（北京时间）" in mail.html
    assert "token=" not in mail.text and "token=" not in mail.html


def test_html_escapes_base_url() -> None:
    mail = render_verify('https://x.test/"><script>', TOKEN, 24)
    assert "<script>" not in mail.html


def test_queue_account_mail_returns_row_id_and_dedupe_key(db: Session) -> None:
    user = User(email="u@example.com")
    db.add(user)
    db.flush()
    mail = render_verify(BASE, TOKEN, 24)
    outbox_id = queue_account_mail(db, user, "account_verify", mail, NOW)
    assert outbox_id is not None
    row = db.scalars(select(Outbox).where(Outbox.id == outbox_id)).one()
    assert row.dedupe_key == f"account_verify:{user.id}:{NOW.isoformat()}"
    assert (row.user_id, row.to_addr, row.kind) == (user.id, user.email, "account_verify")
    assert TOKEN not in row.dedupe_key
    assert queue_account_mail(db, user, "account_verify", mail, NOW) is None


def test_password_changed_wording_depends_on_kept_session() -> None:
    reset = render_password_changed(BASE, NOW, kept_current=False)
    change = render_password_changed(BASE, NOW, kept_current=True)
    for body in (reset.text, reset.html):
        assert "所有设备需要重新登录" in body
        assert "当前浏览器" not in body
    for body in (change.text, change.html):
        assert "其他设备需要重新登录，当前浏览器不受影响" in body
        assert "所有" not in body
