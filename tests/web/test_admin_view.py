"""管理员只读查看某个使用者的手动账、系统账、策略：内容、只读、留痕、权限。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from qt.db import repo
from qt.db.models import AuditLog, LedgerEntry, User
from qt.service import accounts
from qt.service.actor import CLI
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.jobs.conftest import run_two_days
from tests.web.conftest import BASE, MakeUser, World, login_as, world_deps
from tests.web.test_access import owned_digest

ADMIN = "admin@example.com"
USER = "user@example.com"
TABS = ("manual", "system", "strategy")
WRITE_LINKS = (
    "/manual/trade",
    "/manual/holding",
    "/manual/cash",
    "/manual/entries/",
    "/strategy/params",
    "/strategy/close",
    "/strategy/pause",
    "/strategy/new",
)


@pytest.fixture
def deps(world: World, db_engine: Engine, tmp_path: Path) -> WebDeps:
    return world_deps(world, db_engine, tmp_path)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _setup(client: TestClient, make_user: MakeUser) -> tuple[User, User]:
    admin = make_user(ADMIN, admin=True)
    user = make_user(USER)
    assert login_as(client, ADMIN).status_code == 303
    return admin, user


def _views(db: Session) -> list[AuditLog]:
    db.expire_all()
    return list(db.scalars(select(AuditLog).where(AuditLog.action == "admin_view")))


def test_admin_sees_books_and_strategy_read_only_and_each_view_is_logged(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    admin, user = _setup(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    system = repo.get_book_of_kind(db, user.id, sub.id, "system")
    last = repo.list_book_days(db, user.id, system.id)[-1]
    entries_before = len(list(db.scalars(select(LedgerEntry))))

    assert f'href="/admin/users/{user.id}/manual"' in client.get("/admin/users").text
    pages = {}
    for tab in TABS:
        resp = client.get(f"/admin/users/{user.id}/{tab}")
        assert resp.status_code == 200, tab
        pages[tab] = resp.text
        assert f"正在查看 {USER}" in resp.text and "（只读）" in resp.text
        assert "记入操作日志" in resp.text
        for link in WRITE_LINKS:
            assert link not in resp.text, (tab, link)
        assert "建议买入" not in resp.text and "推荐" not in resp.text

    assert "按信号买入" in pages["system"] and f"{last.equity:,.2f}" in pages["system"]
    assert "持仓" in pages["manual"] and "流水" in pages["manual"]
    assert "data-live-holdings" in pages["manual"] and "data-live-holdings" in pages["system"]
    assert f"订阅 #{sub.id}" in pages["strategy"] and "默认值" in pages["strategy"]

    logged = _views(db)
    assert [row.detail["page"] for row in logged] == list(TABS)
    assert all(row.actor_user_id == admin.id and row.channel == "web" for row in logged)
    assert all(row.target == f"user:{user.id}" for row in logged)
    assert len(list(db.scalars(select(LedgerEntry)))) == entries_before


def test_holdings_fragments_are_logged_once_per_book_not_on_every_poll(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    admin, user = _setup(client, make_user)
    run_two_days(world.env, db, user.id)
    # 不经过整页、直接请求持仓表这一块：也要留痕；反复请求（定时刷新）不重复记
    for _ in range(3):
        for book in ("manual", "system"):
            resp = client.get(f"/admin/users/{user.id}/{book}/holdings")
            assert resp.status_code == 200
            assert "<html" not in resp.text and "data-live-holdings" in resp.text
    logged = _views(db)
    assert sorted(row.detail["page"] for row in logged) == ["manual", "system"]
    assert all(row.actor_user_id == admin.id for row in logged)

    # 打开整页之后的定时刷新不再多记；整页每次都记
    assert client.get(f"/admin/users/{user.id}/manual").status_code == 200
    assert client.get(f"/admin/users/{user.id}/manual/holdings").status_code == 200
    assert [row.detail["page"] for row in _views(db)].count("manual") == 2


def test_viewing_changes_nothing_of_the_user(
    client: TestClient, make_user: MakeUser, world: World, db: Session, db_engine: Engine
) -> None:
    _admin, user = _setup(client, make_user)
    run_two_days(world.env, db, user.id)
    before = owned_digest(db_engine, user.id)
    world.clock.now = world.clock.now.replace(hour=10, minute=0, second=0)
    paths = [f"/admin/users/{user.id}/{tab}" for tab in TABS]
    paths += [f"/admin/users/{user.id}/{book}/holdings" for book in ("manual", "system")]
    for path in paths:
        assert client.get(path).status_code == 200, path
    after = owned_digest(db_engine, user.id)
    changed = {table for table in before | after if before.get(table) != after.get(table)}
    assert changed <= {"audit_log"}, changed


def test_closed_subscription_can_still_be_viewed(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    _admin, user = _setup(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    accounts.close_subscription(db, user.id, sub.id, actor=CLI)
    db.commit()
    for tab in TABS:
        resp = client.get(f"/admin/users/{user.id}/{tab}")
        assert resp.status_code == 200, tab
        assert "已关闭" in resp.text
        for link in WRITE_LINKS:
            assert link not in resp.text


def test_live_url_points_at_the_admin_fragment_during_session(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    _admin, user = _setup(client, make_user)
    run_two_days(world.env, db, user.id)
    world.clock.now = world.clock.now.replace(hour=10, minute=0, second=0)
    for book in ("manual", "system"):
        text = client.get(f"/admin/users/{user.id}/{book}").text
        assert f'data-live-url="/admin/users/{user.id}/{book}/holdings"' in text


def test_user_without_subscription_and_unknown_user(
    client: TestClient, make_user: MakeUser
) -> None:
    _admin, user = _setup(client, make_user)
    for tab in TABS:
        resp = client.get(f"/admin/users/{user.id}/{tab}")
        assert resp.status_code == 200 and "这位使用者还没有订阅" in resp.text
        assert client.get(f"/admin/users/999999/{tab}").status_code == 404
    for book in ("manual", "system"):
        assert client.get(f"/admin/users/999999/{book}/holdings").status_code == 404


def test_non_admin_cannot_view_anyone_and_nothing_is_logged(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    admin, user = _setup(client, make_user)
    run_two_days(world.env, db, user.id)
    other = make_user("other@example.com")
    plain = TestClient(client.app, base_url=BASE)
    assert login_as(plain, "other@example.com").status_code == 303
    paths = [f"/admin/users/{uid}/{tab}" for uid in (user.id, other.id, admin.id) for tab in TABS]
    paths += [f"/admin/users/{user.id}/{book}/holdings" for book in ("manual", "system")]
    for path in paths:
        assert plain.get(path).status_code == 404, path
    guest = TestClient(client.app, base_url=BASE)
    for path in paths:
        resp = guest.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    assert _views(db) == []


def test_signals_journal_and_overview_of_others_stay_closed(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    _admin, user = _setup(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    for page in ("signals", "journal", "overview"):
        assert client.get(f"/admin/users/{user.id}/{page}").status_code == 404
        # 管理员用别人的订阅编号走普通页面，照样 404
        assert client.get(f"/{page}?sub={sub.id}").status_code == 404


def test_terms_say_the_admin_can_view_books(client: TestClient) -> None:
    text = client.get("/terms").text
    assert "也可以查看你的订阅设置" in text and "每次查看都会留下记录" in text
