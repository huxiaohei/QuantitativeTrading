"""管理页面：邀请码、使用者启停、运行记录与立即重试；普通使用者一律 404；管理员看不到账本内容。"""

import re
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.db import admin_repo
from qt.db.models import AuditLog, DailyRun, Invite, Outbox, RerunRequest
from qt.service import accounts
from qt.service.actor import CLI
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.web.conftest import (
    BASE,
    CFG,
    PW,
    Clock,
    MakeUser,
    csrf_of,
    login_as,
    make_deps,
)

ADMIN = "admin@example.com"
USER = "user@example.com"
MARKER = "123456.78"
FRIDAY = date(2026, 11, 13)
MONDAY = date(2026, 11, 16)
INVITE_LINK = re.compile(re.escape(BASE) + r"/register\?invite=([A-Za-z0-9_-]+)")


def _calendar() -> TradingCalendar:
    return TradingCalendar([FRIDAY, MONDAY])


@pytest.fixture
def deps(db: Session, db_engine: Engine, tmp_path: Path, clock: Clock) -> WebDeps:
    del db
    return make_deps(db_engine, tmp_path, clock, tmp_path / "mails", calendar=_calendar)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _admin(client: TestClient, make_user: MakeUser) -> int:
    admin = make_user(ADMIN, admin=True)
    assert login_as(client, ADMIN).status_code == 303
    return admin.id


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> httpx.Response:
    token = csrf_of(client.get("/settings"))
    return client.post(path, data={**(data or {}), "csrf": token}, follow_redirects=False)


def _create_invite(client: TestClient, note: str = "给朋友") -> str:
    resp = _post(client, "/admin/invites", {"note": note})
    assert resp.status_code == 303
    page = client.get("/admin/invites")
    found = INVITE_LINK.search(page.text)
    assert found is not None
    return found.group(1)


def _register(client: TestClient, email: str, code: str) -> httpx.Response:
    page = client.get(f"/register?invite={code}")
    return client.post(
        "/register",
        data={
            "email": email,
            "password": PW,
            "invite": code,
            "accept_terms": "on",
            "csrf": csrf_of(page),
        },
        follow_redirects=False,
    )


# ---- 邀请码 ----


def test_invite_link_registers_and_is_single_use(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    admin_id = _admin(client, make_user)
    code = _create_invite(client)
    page = client.get("/admin/invites")
    assert f"{BASE}/register?invite={code}" in page.text and "可用" in page.text
    guest = TestClient(client.app, base_url=BASE)
    # 链接带出邀请码
    assert f'value="{code}"' in guest.get(f"/register?invite={code}").text
    assert _register(guest, "new@example.com", code).status_code == 303
    assert admin_repo.find_user_by_email(db, "new@example.com") is not None
    used = client.get("/admin/invites")
    assert "已使用" in used.text and code not in used.text
    assert _register(guest, "second@example.com", code).status_code == 400
    audits = list(db.scalars(select(AuditLog).where(AuditLog.action == "invite_create")))
    assert len(audits) == 1
    assert audits[0].channel == "web" and audits[0].actor_user_id == admin_id


def test_revoked_invite_cannot_register_and_shows_no_link(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    _admin(client, make_user)
    code = _create_invite(client)
    invite = db.scalars(select(Invite)).one()
    resp = _post(client, f"/admin/invites/{invite.id}/revoke")
    assert resp.status_code == 303
    page = client.get("/admin/invites")
    assert "已作废" in page.text and code not in page.text and "给朋友" in page.text
    guest = TestClient(client.app, base_url=BASE)
    assert _register(guest, "new@example.com", code).status_code == 400
    assert db.scalars(select(AuditLog).where(AuditLog.action == "invite_revoke")).first()


def test_expired_invite_shows_state_without_link(
    client: TestClient, make_user: MakeUser, clock: Clock
) -> None:
    _admin(client, make_user)
    code = _create_invite(client)
    clock.advance(timedelta(days=CFG.web.invite_valid_days + 1))
    page = client.get("/admin/invites")
    assert page.status_code == 200
    assert "已过期" in page.text and code not in page.text


def test_invite_note_too_long_is_422(client: TestClient, make_user: MakeUser) -> None:
    _admin(client, make_user)
    resp = _post(client, "/admin/invites", {"note": "x" * 201})
    assert resp.status_code == 422 and "200" in resp.text


def test_revoke_unknown_invite_is_404(client: TestClient, make_user: MakeUser) -> None:
    _admin(client, make_user)
    assert _post(client, "/admin/invites/999/revoke").status_code == 404


# ---- 使用者 ----


def test_disable_user_kills_sessions_and_blocks_login(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    admin_id = _admin(client, make_user)
    user = make_user(USER)
    victim = TestClient(client.app, base_url=BASE)
    assert login_as(victim, USER).status_code == 303
    confirm = client.get(f"/admin/users/{user.id}/disable")
    assert confirm.status_code == 200 and USER in confirm.text and "停用" in confirm.text
    db.expire_all()
    found = admin_repo.find_user_by_email(db, USER)
    assert found is not None and found.status == "active"  # 打开确认页不会停用
    resp = _post(client, f"/admin/users/{user.id}/disable")
    assert resp.status_code == 303
    assert victim.get("/overview", follow_redirects=False).status_code == 303
    failed = login_as(TestClient(client.app, base_url=BASE), USER)
    assert failed.status_code == 400 and "邮箱或密码不正确" in failed.text
    page = client.get("/admin/users")
    assert "已停用" in page.text
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "user_disable")).one()
    assert audit.channel == "web" and audit.actor_user_id == admin_id
    # 恢复后可以登录
    assert _post(client, f"/admin/users/{user.id}/enable").status_code == 303
    assert login_as(TestClient(client.app, base_url=BASE), USER).status_code == 303
    assert db.scalars(select(AuditLog).where(AuditLog.action == "user_enable")).one()


def test_cannot_disable_self(client: TestClient, make_user: MakeUser) -> None:
    admin_id = _admin(client, make_user)
    resp = _post(client, f"/admin/users/{admin_id}/disable")
    assert resp.status_code == 422 and "不能停用自己" in resp.text
    assert client.get("/admin/users").status_code == 200


def test_users_page_lists_only_account_facts(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    _admin(client, make_user)
    user = make_user(USER)
    make_user("fresh@example.com", verified=False)
    sub = accounts.create_subscription(db, user.id, CFG, float(MARKER), MONDAY, actor=CLI)
    db.commit()
    assert sub.initial_cash == float(MARKER)
    pages = [client.get(p).text for p in ("/admin/users", "/admin/runs", "/admin/invites")]
    for text in pages:
        assert MARKER not in text and "123,456.78" not in text and "123456" not in text
    users = pages[0]
    assert USER in users and "fresh@example.com" in users
    assert "生效中" in users and "未验证" in users


# ---- 运行记录与重试 ----


def test_runs_page_shows_failure_reason_outbox_and_retry_state(
    client: TestClient, make_user: MakeUser, db: Session, clock: Clock
) -> None:
    admin_id = _admin(client, make_user)
    db.add(
        DailyRun(
            trade_day=FRIDAY,
            status="data_failed",
            stage="data",
            started_at=clock.now,
            detail={"error": "ConnectionError: 数据源连不上-完整原因"},
        )
    )
    db.add(
        Outbox(
            to_addr="x@example.com",
            kind="k",
            dedupe_key="k1",
            subject="s",
            body_html="h",
            body_text="t",
            status="failed",
        )
    )
    db.commit()
    page = client.get("/admin/runs")
    assert page.status_code == 200
    assert "数据源连不上-完整原因" in page.text and str(FRIDAY) in page.text
    assert "发送失败" in page.text and "没有重试请求" in page.text
    # 请求一次重试之后显示待处理
    clock.advance(timedelta(hours=10))  # 19:00，周一已到运行时间
    accounts.create_subscription(db, admin_id, CFG, 1000.0, MONDAY, actor=CLI)
    db.commit()
    assert _post(client, "/admin/runs/retry").status_code == 303
    assert "等待 worker 处理" in client.get("/admin/runs").text
    _post(client, "/admin/runs/retry")
    db.expire_all()
    assert len(list(db.scalars(select(RerunRequest)))) == 1
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "rerun_request")).one()
    assert audit.channel == "web" and audit.actor_user_id == admin_id


def test_retry_with_nothing_unfinished_is_422(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    _admin(client, make_user)
    resp = _post(client, "/admin/runs/retry")
    assert resp.status_code == 422 and "没有未完成的交易日" in resp.text
    assert db.scalars(select(RerunRequest)).first() is None


def test_requeue_failed_mail(client: TestClient, make_user: MakeUser, db: Session) -> None:
    admin_id = _admin(client, make_user)
    db.add(
        Outbox(
            to_addr="x@example.com",
            kind="k",
            dedupe_key="k1",
            subject="s",
            body_html="h",
            body_text="t",
            status="failed",
        )
    )
    db.commit()
    assert _post(client, "/admin/outbox/requeue").status_code == 303
    db.expire_all()
    assert db.scalars(select(Outbox)).one().status == "pending"
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "outbox_requeue")).one()
    assert audit.channel == "web" and audit.actor_user_id == admin_id


# ---- 权限 ----

ADMIN_PAGES = ("/admin/invites", "/admin/users", "/admin/runs", "/admin/users/1/disable")
ADMIN_POSTS = (
    "/admin/invites",
    "/admin/invites/1/revoke",
    "/admin/users/1/disable",
    "/admin/users/1/enable",
    "/admin/runs/retry",
    "/admin/outbox/requeue",
)


def test_non_admin_gets_404_everywhere(client: TestClient, make_user: MakeUser) -> None:
    make_user(USER)
    login_as(client, USER)
    for path in ADMIN_PAGES:
        assert client.get(path).status_code == 404, path
    for path in ADMIN_POSTS:
        assert _post(client, path).status_code == 404, path
    assert "/admin" not in client.get("/settings").text


def test_admin_pages_require_login(client: TestClient) -> None:
    resp = client.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_admin_nav_link_only_for_admin(client: TestClient, make_user: MakeUser) -> None:
    _admin(client, make_user)
    assert 'href="/admin/invites"' in client.get("/settings").text
