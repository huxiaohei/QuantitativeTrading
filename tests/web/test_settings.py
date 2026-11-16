"""设置页面：邮件开关、改密码、退出所有设备。"""

from pathlib import Path

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from qt.db import admin_repo
from qt.db.models import AuditLog, WebSession
from tests.web.conftest import (
    BASE,
    PW,
    MakeUser,
    csrf_of,
    login_as,
    mail_text,
    read_mails,
)

EMAIL = "a@example.com"
NEW_PW = "another-long-password"


def _post(client: TestClient, path: str, data: dict[str, str]) -> httpx.Response:
    token = csrf_of(client.get("/settings"))
    return client.post(path, data={**data, "csrf": token}, follow_redirects=False)


def _user(db: Session) -> tuple[bool, bool]:
    db.expire_all()
    user = admin_repo.find_user_by_email(db, EMAIL)
    assert user is not None
    return user.weekly_mail, user.abandon_mail


def _session_count(engine: Engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(WebSession)) or 0


def test_settings_requires_login(client: TestClient) -> None:
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fsettings"


def test_settings_page_shows_current_switches(client: TestClient, make_user: MakeUser) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    page = client.get("/settings")
    assert page.status_code == 200
    assert 'name="weekly_mail"' in page.text and 'name="abandon_mail"' in page.text
    assert page.text.count("checked") == 2
    assert "不构成投资建议" in page.text


def test_mail_switches_work_independently(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    resp = _post(client, "/settings/mail", {"weekly_mail": "on"})
    assert resp.status_code == 303 and resp.headers["location"].startswith("/settings")
    assert _user(db) == (True, False)
    _post(client, "/settings/mail", {"abandon_mail": "on"})
    assert _user(db) == (False, True)
    _post(client, "/settings/mail", {})
    assert _user(db) == (False, False)
    page = client.get(_post(client, "/settings/mail", {"weekly_mail": "on"}).headers["location"])
    assert "设置已保存" in page.text
    audits = list(db.scalars(select(AuditLog).where(AuditLog.action == "user.weekly_mail")))
    assert audits and all(a.channel == "web" for a in audits)


def test_change_password_success(
    client: TestClient, make_user: MakeUser, db_engine: Engine
) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    other = TestClient(client.app, base_url=BASE)
    assert login_as(other, EMAIL).status_code == 303
    assert _session_count(db_engine) == 2
    resp = _post(client, "/settings/password", {"current": PW, "new": NEW_PW})
    assert resp.status_code == 303
    assert _session_count(db_engine) == 1
    # 当前浏览器仍然有效，另一个客户端的会话失效
    assert client.get("/settings", follow_redirects=False).status_code == 200
    assert other.get("/settings", follow_redirects=False).status_code == 303
    # 旧密码登不上，新密码可以
    fresh = TestClient(client.app, base_url=BASE)
    assert login_as(fresh, EMAIL, PW).status_code == 400
    assert login_as(fresh, EMAIL, NEW_PW).status_code == 303


def test_change_password_sends_notice_mail(
    client: TestClient, make_user: MakeUser, mail_dir: Path
) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    _post(client, "/settings/password", {"current": PW, "new": NEW_PW})
    mails = read_mails(mail_dir, EMAIL)
    assert [m["Subject"] for m in mails] == ["【账号提示】你的密码已修改"]
    assert NEW_PW not in str(mails[0]) and PW not in str(mails[0])
    assert "其他设备需要重新登录，当前浏览器不受影响" in mail_text(mails[0])


def test_change_password_wrong_current_is_422(
    client: TestClient, make_user: MakeUser, mail_dir: Path
) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    resp = _post(client, "/settings/password", {"current": "wrong-password-x", "new": NEW_PW})
    assert resp.status_code == 422
    assert "当前密码不正确" in resp.text
    assert read_mails(mail_dir, EMAIL) == []
    fresh = TestClient(client.app, base_url=BASE)
    assert login_as(fresh, EMAIL, PW).status_code == 303  # 密码没变


def test_change_password_rejects_weak_and_too_long(client: TestClient, make_user: MakeUser) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    short = _post(client, "/settings/password", {"current": PW, "new": "short"})
    assert short.status_code == 422 and "至少" in short.text
    long = _post(client, "/settings/password", {"current": PW, "new": "x" * 1025})
    assert long.status_code == 422 and "1024" in long.text


def test_logout_all_clears_every_session(
    client: TestClient, make_user: MakeUser, db_engine: Engine
) -> None:
    make_user(EMAIL)
    login_as(client, EMAIL)
    other = TestClient(client.app, base_url=BASE)
    login_as(other, EMAIL)
    resp = _post(client, "/settings/logout-all", {})
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    cookies = [h for h in resp.headers.get_list("set-cookie") if h.startswith("__Host-qt_session=")]
    assert len(cookies) == 1 and "max-age=0" in cookies[0].lower()
    assert _session_count(db_engine) == 0
    assert client.get("/settings", follow_redirects=False).status_code == 303
    assert other.get("/settings", follow_redirects=False).status_code == 303
