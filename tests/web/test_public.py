"""公开页面：首页跳转、登录、注册、验证邮箱、重发验证、找回与重置密码、条款、退出、健康检查。"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from qt.db import admin_repo
from qt.db.models import Invite, WebSession
from tests.web.conftest import (
    CFG,
    PW,
    Clock,
    MakeUser,
    csrf_of,
    link_path,
    login_as,
    read_mails,
)

INVALID_LINK = "链接已使用或已过期"
RESEND_SENT = "如果该邮箱已注册且尚未验证，验证邮件已重新发出"
FORGOT_SENT = "如果该邮箱已注册，重置邮件已发出"
NEW_PW = "another-long-password"

LastMail = Callable[[str], str]
InviteCode = Callable[[], str]


def _form_post(
    client: TestClient, page_path: str, action: str, data: dict[str, str]
) -> httpx.Response:
    """先打开表单页取令牌，再提交（不跟随跳转）。"""
    page = client.get(page_path)
    return client.post(action, data={**data, "csrf": csrf_of(page)}, follow_redirects=False)


def _register(
    client: TestClient, email_addr: str, code: str, password: str = PW, accept: bool = True
) -> httpx.Response:
    data = {"email": email_addr, "password": password, "invite": code}
    if accept:
        data["accept_terms"] = "on"
    return _form_post(client, "/register", "/register", data)


def _session_count(engine: Engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(WebSession)) or 0


# ---- 首页与健康检查 ----


def test_root_redirects_to_login_when_anonymous(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_root_redirects_to_overview_when_logged_in(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    assert login_as(client, "foo@example.com").status_code == 303
    resp = client.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/overview"


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ---- 登录 ----


def test_login_page_has_form(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'action="/login"' in resp.text
    assert csrf_of(resp)
    assert 'href="/forgot"' in resp.text
    assert 'href="/register"' in resp.text


def test_login_success_redirects_to_overview(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    resp = login_as(client, "Foo@Example.com ")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/overview"
    page = client.get("/overview")
    assert page.status_code == 200
    assert "foo@example.com" in page.text


@pytest.mark.parametrize("email_addr", ["foo@example.com", "nobody@example.com"])
def test_login_failure_same_message(
    client: TestClient, make_user: MakeUser, email_addr: str
) -> None:
    make_user("foo@example.com")
    resp = login_as(client, email_addr, "wrong-password!")
    assert resp.status_code == 400
    assert "邮箱或密码不正确" in resp.text
    assert 'value="' + email_addr + '"' in resp.text
    assert "wrong-password!" not in resp.text


def test_login_unverified_shows_resend_form(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com", verified=False)
    resp = login_as(client, "foo@example.com")
    assert resp.status_code == 400
    assert "请先验证邮箱" in resp.text
    assert 'action="/verify/resend"' in resp.text
    assert "set-cookie" not in resp.headers or "qt_session" not in resp.headers["set-cookie"]


def test_login_password_too_long_is_form_error(client: TestClient) -> None:
    resp = login_as(client, "foo@example.com", "x" * 1025)
    assert resp.status_code == 400
    assert "密码不能超过 1024 个字符" in resp.text


def test_login_follows_safe_next(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com")
    page = client.get("/login", params={"next": "/signals?x=1"})
    assert 'value="/signals?x=1"' in page.text
    resp = client.post(
        "/login",
        data={
            "email": "foo@example.com",
            "password": PW,
            "next": "/signals?x=1",
            "csrf": csrf_of(page),
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/signals?x=1"


@pytest.mark.parametrize(
    "bad_next", ["//evil.example", "https://evil.example", "/\\evil.example", "evil", ""]
)
def test_login_ignores_unsafe_next(client: TestClient, make_user: MakeUser, bad_next: str) -> None:
    make_user("foo@example.com")
    page = client.get("/login", params={"next": bad_next})
    assert "evil.example" not in page.text
    resp = client.post(
        "/login",
        data={"email": "foo@example.com", "password": PW, "next": bad_next, "csrf": csrf_of(page)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/overview"


def test_login_flash_only_whitelisted_codes(client: TestClient) -> None:
    assert "邮箱已验证，请登录" in client.get("/login?msg=verified").text
    assert "登录已过期，请重新登录" in client.get("/login?msg=expired").text
    page = client.get("/login", params={"msg": "<b>随便写</b>"})
    assert page.status_code == 200
    assert "随便写" not in page.text


def test_login_rate_limit_is_committed(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    make_user("foo@example.com")
    for _ in range(6):
        assert login_as(client, "foo@example.com", "wrong-password!").status_code == 400
    resp = login_as(client, "foo@example.com")
    assert resp.status_code == 400
    assert "操作太频繁，请稍后再试" in resp.text
    clock.advance(timedelta(minutes=15))
    resp = login_as(client, "foo@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/overview"


# ---- 注册 ----


def test_register_page(client: TestClient) -> None:
    resp = client.get("/register")
    assert resp.status_code == 200
    assert 'action="/register"' in resp.text
    assert 'href="/terms"' in resp.text


def test_register_new_and_existing_look_identical(
    client: TestClient,
    make_user: MakeUser,
    invite_code: InviteCode,
    last_mail: LastMail,
) -> None:
    make_user("old@example.com")
    first = _register(client, "new@example.com", invite_code())
    second = _register(client, "old@example.com", invite_code())
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"] == "/register/sent"
    for resp in (first, second):
        assert "qt_session" not in resp.headers.get("set-cookie", "")
    page1 = client.get(first.headers["location"])
    page2 = client.get(second.headers["location"])
    assert page1.status_code == page2.status_code == 200
    assert page1.content == page2.content
    assert "/verify?token=" in last_mail("new@example.com")
    assert "已经注册过" in last_mail("old@example.com")


def test_register_existing_does_not_use_invite(
    client: TestClient, make_user: MakeUser, invite_code: InviteCode, db_engine: Engine
) -> None:
    make_user("old@example.com")
    code = invite_code()
    _register(client, "old@example.com", code)
    with Session(db_engine) as s:
        invite = s.scalars(select(Invite).where(Invite.code == code)).one()
        assert invite.used_by is None


@pytest.mark.parametrize(
    ("password", "accept", "code", "message"),
    [
        (PW, True, "bad-code", "邀请码无效或已过期"),
        (PW, False, None, "请先阅读并同意使用条款与风险提示"),
        ("short", True, None, "密码至少 10 位"),
        ("x" * 1025, True, None, "密码不能超过 1024 个字符"),
    ],
)
def test_register_errors_keep_input(
    client: TestClient,
    invite_code: InviteCode,
    mail_dir: Path,
    password: str,
    accept: bool,
    code: str | None,
    message: str,
) -> None:
    real_code = code if code is not None else invite_code()
    resp = _register(client, "new@example.com", real_code, password, accept)
    assert resp.status_code == 400
    assert message in resp.text
    assert 'value="new@example.com"' in resp.text
    assert password not in resp.text
    assert not read_mails(mail_dir, "new@example.com")


def test_full_flow_mail_link_verify_then_login(
    client: TestClient, invite_code: InviteCode, last_mail: LastMail
) -> None:
    assert _register(client, "new@example.com", invite_code()).status_code == 303
    verify_path = link_path(last_mail("new@example.com"), "/verify")
    page = client.get(verify_path)
    assert page.status_code == 200
    assert "确认验证" in page.text
    resp = client.post(
        "/verify",
        data={"token": verify_path.split("token=")[1], "csrf": csrf_of(page)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?msg=verified"
    assert "邮箱已验证，请登录" in client.get(resp.headers["location"]).text
    assert login_as(client, "new@example.com").headers["location"] == "/overview"
    assert client.get("/overview").status_code == 200


# ---- 验证邮箱 ----


def test_verify_get_does_not_consume_and_post_once(
    client: TestClient, invite_code: InviteCode, last_mail: LastMail
) -> None:
    _register(client, "new@example.com", invite_code())
    verify_path = link_path(last_mail("new@example.com"), "/verify")
    token = verify_path.split("token=")[1]
    client.get(verify_path)
    page = client.get(verify_path)
    assert "确认验证" in page.text
    data = {"token": token, "csrf": csrf_of(page)}
    assert client.post("/verify", data=data, follow_redirects=False).status_code == 303
    again = client.post("/verify", data=data, follow_redirects=False)
    assert again.status_code in (200, 410)
    assert INVALID_LINK in again.text


def test_verify_bad_token(client: TestClient) -> None:
    resp = client.get("/verify", params={"token": "nope"})
    assert resp.status_code in (200, 410)
    assert INVALID_LINK in resp.text
    assert "确认验证" not in resp.text
    assert client.get("/verify").status_code in (200, 410)


# ---- 重发验证邮件 ----


def _resend(client: TestClient, email_addr: str) -> httpx.Response:
    return _form_post(client, "/login", "/verify/resend", {"email": email_addr})


def test_resend_for_unverified_user_sends_mail(
    client: TestClient, make_user: MakeUser, mail_dir: Path, last_mail: LastMail
) -> None:
    make_user("foo@example.com", verified=False)
    resp = _resend(client, "foo@example.com")
    assert resp.status_code == 200
    assert RESEND_SENT in resp.text
    assert "/verify?token=" in last_mail("foo@example.com")
    assert len(read_mails(mail_dir, "foo@example.com")) == 1


def test_resend_uniform_for_unknown_verified_and_rate_limited(
    client: TestClient, make_user: MakeUser, mail_dir: Path, clock: Clock
) -> None:
    make_user("done@example.com")
    make_user("foo@example.com", verified=False)
    bodies = {
        _resend(client, "nobody@example.com").text,
        _resend(client, "done@example.com").text,
    }
    for _ in range(4):
        clock.advance(timedelta(seconds=1))  # 账号邮件的去重键含发信时刻
        resp = _resend(client, "foo@example.com")
        assert resp.status_code == 200
        bodies.add(resp.text)
    assert len(bodies) == 1
    assert RESEND_SENT in bodies.pop()
    assert len(read_mails(mail_dir, "foo@example.com")) == 3
    assert not read_mails(mail_dir, "done@example.com")
    assert not read_mails(mail_dir, "nobody@example.com")


# ---- 找回与重置密码 ----


def _forgot(client: TestClient, email_addr: str) -> httpx.Response:
    return _form_post(client, "/forgot", "/forgot", {"email": email_addr})


def test_forgot_same_page_whether_or_not_registered(
    client: TestClient, make_user: MakeUser, mail_dir: Path
) -> None:
    make_user("foo@example.com")
    assert client.get("/forgot").status_code == 200
    known = _forgot(client, "foo@example.com")
    unknown = _forgot(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content
    assert FORGOT_SENT in known.text
    assert len(read_mails(mail_dir, "foo@example.com")) == 1
    assert not read_mails(mail_dir, "nobody@example.com")


def test_reset_flow(
    client: TestClient,
    make_user: MakeUser,
    last_mail: LastMail,
    db_engine: Engine,
) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    assert _session_count(db_engine) == 1
    _forgot(client, "foo@example.com")
    reset_path = link_path(last_mail("foo@example.com"), "/reset")
    token = reset_path.split("token=")[1]
    page = client.get(reset_path)
    assert page.status_code == 200
    assert 'name="password"' in page.text

    short = client.post("/reset", data={"token": token, "password": "short", "csrf": csrf_of(page)})
    assert short.status_code == 400
    assert "密码至少 10 位" in short.text

    resp = client.post(
        "/reset",
        data={"token": token, "password": NEW_PW, "csrf": csrf_of(page)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?msg=reset"
    assert "密码已修改" in last_mail("foo@example.com")
    assert "所有设备需要重新登录" in last_mail("foo@example.com")
    assert _session_count(db_engine) == 0
    assert login_as(client, "foo@example.com", PW).status_code == 400
    assert login_as(client, "foo@example.com", NEW_PW).status_code == 303

    page = client.get("/login")
    again = client.post("/reset", data={"token": token, "password": NEW_PW, "csrf": csrf_of(page)})
    assert again.status_code in (200, 410)
    assert INVALID_LINK in again.text


def test_reset_bad_token(client: TestClient) -> None:
    resp = client.get("/reset", params={"token": "nope"})
    assert resp.status_code in (200, 410)
    assert INVALID_LINK in resp.text
    assert 'name="password"' not in resp.text


def test_reset_verifies_email_of_unverified_user(
    client: TestClient, make_user: MakeUser, last_mail: LastMail, db_engine: Engine
) -> None:
    make_user("foo@example.com", verified=False)
    _forgot(client, "foo@example.com")
    reset_path = link_path(last_mail("foo@example.com"), "/reset")
    page = client.get(reset_path)
    client.post(
        "/reset",
        data={"token": reset_path.split("token=")[1], "password": NEW_PW, "csrf": csrf_of(page)},
    )
    with Session(db_engine) as s:
        user = admin_repo.find_user_by_email(s, "foo@example.com")
        assert user is not None
        assert user.email_verified_at is not None


# ---- 条款 ----


def test_terms_page_public(client: TestClient) -> None:
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert CFG.web.terms_version in resp.text
    assert "本页不是法律意见" in resp.text
    assert "不构成投资建议" in resp.text


def test_new_terms_version_requires_accept(client: TestClient, make_user: MakeUser) -> None:
    make_user("foo@example.com", terms_version="2000-01")
    assert login_as(client, "foo@example.com").status_code == 303
    resp = client.get("/overview", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/terms/accept"
    page = client.get("/terms/accept")
    assert page.status_code == 200
    assert CFG.web.terms_version in page.text

    refused = client.post("/terms/accept", data={"csrf": csrf_of(page)})
    assert refused.status_code == 400
    assert client.get("/overview", follow_redirects=False).status_code == 303

    ok = client.post(
        "/terms/accept", data={"accept": "on", "csrf": csrf_of(page)}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/overview"
    assert client.get("/overview", follow_redirects=False).status_code == 200


def test_terms_accept_requires_login(client: TestClient) -> None:
    resp = client.get("/terms/accept", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


# ---- 退出 ----


def test_logout(client: TestClient, make_user: MakeUser, db_engine: Engine) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    page = client.get("/overview")
    resp = client.post("/logout", data={"csrf": csrf_of(page)}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert _session_count(db_engine) == 0
    after = client.get("/overview", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"] == "/login?next=%2Foverview"


def test_logout_after_session_extension_clears_cookie(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    make_user("foo@example.com")
    login_as(client, "foo@example.com")
    page = client.get("/overview")
    clock.advance(timedelta(minutes=6))  # 本次请求会顺延会话，中间件会想刷新 Cookie
    resp = client.post("/logout", data={"csrf": csrf_of(page)}, follow_redirects=False)
    assert resp.status_code == 303
    session_cookies = [
        h for h in resp.headers.get_list("set-cookie") if h.startswith("__Host-qt_session=")
    ]
    assert len(session_cookies) == 1, session_cookies
    assert "max-age=0" in session_cookies[0].lower()
    assert client.get("/overview", follow_redirects=False).status_code == 303
