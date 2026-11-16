"""访问控制总表（设计 6.7）：路由登记与期望表、未登录、非管理员、甲乙隔离、防伪造、页面不串号。

新增路由时必须同时更新本文件的 EXPECTED；带编号参数的路由还必须登记到 USER_IDS / ADMIN_IDS，
否则这里的测试失败。
"""

import hashlib
import inspect
import types
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Union, get_args, get_origin, get_type_hints
from urllib.parse import quote, urlencode

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import ColumnElement, Engine, Table, or_, select
from sqlalchemy.orm import Session
from starlette.routing import Mount

from qt.db import repo
from qt.db.models import Base, Invite, LedgerEntry, Signal, Subscription
from qt.service import auth, manual_ops
from qt.service.actor import CLI
from qt.service.market import build_store_market
from qt.web.app import ROUTES, Access, create_app
from qt.web.deps import WebDeps
from tests.jobs.conftest import CFG, DAY, run_two_days
from tests.web.conftest import (
    BASE,
    MakeUser,
    World,
    csrf_of,
    login_as,
    subscribe,
    world_deps,
)

Key = tuple[str, str]
IdKind = Literal["subscription", "entry", "signal", "user", "invite"]

# 每条路由应属的类别；与 ROUTES 必须完全相等
EXPECTED: dict[Key, Access] = {
    ("GET", "/"): "public",
    ("GET", "/healthz"): "public",
    ("GET", "/login"): "public",
    ("POST", "/login"): "public",
    ("GET", "/register"): "public",
    ("POST", "/register"): "public",
    ("GET", "/register/sent"): "public",
    ("GET", "/verify"): "public",
    ("POST", "/verify"): "public",
    ("POST", "/verify/resend"): "public",
    ("GET", "/forgot"): "public",
    ("POST", "/forgot"): "public",
    ("GET", "/reset"): "public",
    ("POST", "/reset"): "public",
    ("GET", "/terms"): "public",
    ("POST", "/logout"): "user",
    ("GET", "/terms/accept"): "user",
    ("POST", "/terms/accept"): "user",
    ("GET", "/overview"): "user",
    ("GET", "/signals"): "user",
    ("GET", "/system"): "user",
    ("GET", "/journal"): "user",
    ("GET", "/manual"): "user",
    ("GET", "/manual/trade"): "user",
    ("POST", "/manual/trade"): "user",
    ("GET", "/manual/holding"): "user",
    ("POST", "/manual/holding"): "user",
    ("GET", "/manual/cash"): "user",
    ("POST", "/manual/cash"): "user",
    ("GET", "/manual/entries/{entry_id}/void"): "user",
    ("POST", "/manual/entries/{entry_id}/void"): "user",
    ("GET", "/strategy"): "user",
    ("GET", "/strategy/new"): "user",
    ("POST", "/strategy/new"): "user",
    ("GET", "/strategy/params"): "user",
    ("POST", "/strategy/params"): "user",
    ("POST", "/strategy/pause"): "user",
    ("POST", "/strategy/resume"): "user",
    ("GET", "/strategy/close"): "user",
    ("POST", "/strategy/close"): "user",
    ("GET", "/concepts"): "user",
    ("GET", "/report"): "user",
    ("GET", "/settings"): "user",
    ("POST", "/settings/mail"): "user",
    ("POST", "/settings/password"): "user",
    ("POST", "/settings/logout-all"): "user",
    ("GET", "/admin/invites"): "admin",
    ("POST", "/admin/invites"): "admin",
    ("POST", "/admin/invites/{invite_id}/revoke"): "admin",
    ("GET", "/admin/users"): "admin",
    ("GET", "/admin/users/{user_id}/disable"): "admin",
    ("POST", "/admin/users/{user_id}/disable"): "admin",
    ("POST", "/admin/users/{user_id}/enable"): "admin",
    ("GET", "/admin/runs"): "admin",
    ("POST", "/admin/runs/retry"): "admin",
    ("POST", "/admin/outbox/requeue"): "admin",
}

# 需登录的路由里路径或查询带编号的参数，及编号的种类（甲用乙的编号必须 404）
USER_IDS: dict[Key, dict[str, IdKind]] = {
    ("GET", "/overview"): {"sub": "subscription"},
    ("GET", "/signals"): {"sub": "subscription"},
    ("GET", "/system"): {"sub": "subscription"},
    ("GET", "/journal"): {"sub": "subscription"},
    ("GET", "/manual"): {"entry": "entry"},
    ("GET", "/manual/trade"): {"signal": "signal"},
    ("GET", "/manual/entries/{entry_id}/void"): {"entry_id": "entry"},
    ("POST", "/manual/entries/{entry_id}/void"): {"entry_id": "entry"},
}

# 管理员路由里带编号的参数（非管理员用任何编号都 404）
ADMIN_IDS: dict[Key, dict[str, IdKind]] = {
    ("GET", "/admin/users/{user_id}/disable"): {"user_id": "user"},
    ("POST", "/admin/users/{user_id}/disable"): {"user_id": "user"},
    ("POST", "/admin/users/{user_id}/enable"): {"user_id": "user"},
    ("POST", "/admin/invites/{invite_id}/revoke"): {"invite_id": "invite"},
}

# 摘要覆盖的表：凡是与使用者有关联（直接或经订阅、账本）的表都要在里面；只有 daily_runs 是全站共用的
OWNED_TABLES = {
    "users",
    "subscriptions",
    "param_changes",
    "books",
    "system_states",
    "book_days",
    "ledger_entries",
    "signals",
    "rejections",
    "risk_events",
    "trades",
    "followups",
    "outbox",
    "audit_log",
    "invites",
    "web_sessions",
    "email_tokens",
    "rate_events",
    "rerun_requests",
}
SHARED_TABLES = {"daily_runs"}

B_EMAIL = "zeta.marker.b@example.com"
B_MARKER = 87654.32  # 乙手动账里的特征金额（入金）
B_MARKER_TEXTS = ("87,654.32", "87654.32")


# ---- 路由与参数 ----


def _key(route: APIRoute) -> Key:
    assert route.methods is not None and len(route.methods) == 1
    return (next(iter(route.methods)), route.path)


def _app_routes(app: FastAPI) -> dict[Key, APIRoute]:
    found = [r for r in app.routes if isinstance(r, APIRoute)]
    table = {_key(r): r for r in found}
    assert len(table) == len(found)
    return table


def _is_int(annotation: object) -> bool:
    """参数类型是否为 int（含 int | None、Annotated[int, …]）。"""
    if get_origin(annotation) is Annotated:
        return _is_int(get_args(annotation)[0])
    if get_origin(annotation) in (Union, types.UnionType):
        return any(_is_int(arg) for arg in get_args(annotation))
    return annotation is int


# 名字像编号的参数：无论标注成什么类型都算编号（防止把编号声明成 str 绕过本表）
_ID_NAMES = frozenset({"sub", "signal", "entry"})


def _looks_like_id(name: str) -> bool:
    return name in _ID_NAMES or name.endswith("id")


def _id_params(route: APIRoute) -> set[str]:
    """路由里的编号参数：类型为整数的（路径、查询或表单），以及路径、查询里名字像编号的。"""
    hints = get_type_hints(route.endpoint, include_extras=True)
    names = inspect.signature(route.endpoint).parameters
    found = {name for name in names if _is_int(hints.get(name))}
    for param in (*route.dependant.path_params, *route.dependant.query_params):
        if _looks_like_id(param.name) or _looks_like_id(param.alias):
            found.add(param.name)
    return found


def _url(path: str, params: dict[str, int]) -> str:
    """把编号填进路径占位符，其余放进查询串。"""
    query: dict[str, int] = {}
    for name, value in params.items():
        holder = "{" + name + "}"
        if holder in path:
            path = path.replace(holder, str(value))
        else:
            query[name] = value
    assert "{" not in path, path
    return path + ("?" + urlencode(query) if query else "")


def _placeholder(path: str) -> str:
    """路径里的编号一律填 1（只用来检查登录、管理员与防伪造）。"""
    return _url(path, {name: 1 for name in _path_names(path)})


def _path_names(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{")]


def _routes(access: Access, method: str | None = None) -> list[Key]:
    return sorted(
        (r.method, r.path)
        for r in ROUTES
        if r.access == access and (method is None or r.method == method)
    )


# ---- 1、2：登记表与期望表 ----


def test_every_route_is_registered_and_classified(web_deps: WebDeps) -> None:
    app = create_app(web_deps)
    before = list(ROUTES)
    create_app(web_deps)
    assert before == ROUTES  # 建第二个应用不会重复登记
    registered = {(r.method, r.path): r.access for r in ROUTES}
    assert len(registered) == len(ROUTES)
    # 应用里实际挂上的路由（/static 是挂载，不是路由）与登记表一一对应
    assert set(_app_routes(app)) == set(registered)
    others = [r for r in app.routes if not isinstance(r, APIRoute)]
    assert len(others) == 1 and isinstance(others[0], Mount) and others[0].path == "/static"
    assert registered == EXPECTED


def test_every_id_parameter_is_in_an_isolation_table(web_deps: WebDeps) -> None:
    """路由里每个编号参数都必须登记在 USER_IDS / ADMIN_IDS；公开路由不得接受编号。"""
    access: dict[Key, Access] = {(r.method, r.path): r.access for r in ROUTES}
    found: dict[Access, dict[Key, set[str]]] = {"public": {}, "user": {}, "admin": {}}
    for key, route in _app_routes(create_app(web_deps)).items():
        params = _id_params(route)
        if params:
            found[access[key]][key] = params
    assert found["public"] == {}
    assert found["user"] == {k: set(v) for k, v in USER_IDS.items()}
    assert found["admin"] == {k: set(v) for k, v in ADMIN_IDS.items()}


def test_id_detection_flags_id_names_of_any_type() -> None:
    """名字像编号的路径、查询参数即使标注成 str 也会被认出来；普通参数不会。"""

    def endpoint(item_id: str, sub: str = "", signal: str = "", msg: str = "") -> None:
        del item_id, sub, signal, msg

    probe = APIRoute("/probe/{item_id}", endpoint, methods=["GET"])
    assert _id_params(probe) == {"item_id", "sub", "signal"}


# ---- 3：未登录 ----


def test_anonymous_is_sent_to_login(client: TestClient) -> None:
    token = csrf_of(client.get("/login"))  # 未登录时合法的双重提交令牌
    protected = _routes("user") + _routes("admin")
    for method, path in protected:
        url = _placeholder(path)
        if method == "GET":
            resp = client.get(url + "?a=1", follow_redirects=False)
            assert resp.status_code == 303, (method, path)
            assert resp.headers["location"] == "/login?next=" + quote(url + "?a=1", safe="")
        else:
            resp = client.post(url, data={"csrf": token}, follow_redirects=False)
            assert resp.status_code == 303, (method, path)
            assert resp.headers["location"] == "/login?msg=expired", (method, path)


# ---- 4：非管理员 ----


def test_non_admin_gets_404_on_every_admin_route(client: TestClient, make_user: MakeUser) -> None:
    make_user("a@example.com")
    assert login_as(client, "a@example.com").status_code == 303
    token = csrf_of(client.get("/settings"))
    admin_routes = _routes("admin")
    for method, path in admin_routes:
        url = _placeholder(path)
        if method == "GET":
            resp = client.get(url, follow_redirects=False)
        else:
            resp = client.post(url, data={"csrf": token}, follow_redirects=False)
        assert resp.status_code == 404, (method, path)
        assert "页面不存在" in resp.text


# ---- 6：防伪造 ----


def test_every_post_without_csrf_is_403(web_deps: WebDeps, make_user: MakeUser) -> None:
    make_user("a@example.com")
    make_user("boss@example.com", admin=True)
    app = create_app(web_deps)
    with ExitStack() as stack:
        anon = stack.enter_context(TestClient(app, base_url=BASE))
        anon.get("/login")  # 有防伪造 Cookie，只是表单里不带 csrf
        user = stack.enter_context(TestClient(app, base_url=BASE))
        assert login_as(user, "a@example.com").status_code == 303
        admin = stack.enter_context(TestClient(app, base_url=BASE))
        assert login_as(admin, "boss@example.com").status_code == 303
        clients: dict[Access, TestClient] = {"public": anon, "user": user, "admin": admin}
        posts = [r for r in ROUTES if r.method == "POST"]
        for r in posts:
            resp = clients[r.access].post(
                _placeholder(r.path), data={"x": "1"}, follow_redirects=False
            )
            assert resp.status_code == 403, (r.method, r.path)
        # 403 之后什么都没发生：两人都还登录着
        assert user.get("/settings", follow_redirects=False).status_code == 200
        assert admin.get("/admin/users", follow_redirects=False).status_code == 200


# ---- 5、7：甲乙隔离 ----


def _owned_conditions(
    table: Table, user_id: int, sub_ids: list[int], book_ids: list[int]
) -> list[ColumnElement[bool]]:
    """table 里属于 user_id 的行的条件：指向 users 的外键、经订阅或账本的外键、本人这一行。"""
    conds: list[ColumnElement[bool]] = []
    if table.name == "users":
        conds.append(table.c.id == user_id)
    if table.name == "rate_events":  # 表单一次性值的键里带使用者编号（form:{user_id}:{值}）
        conds.append(table.c.key.like(f"form:{user_id}:%"))
    for column in table.columns:
        for fk in column.foreign_keys:
            target = fk.column.table.name
            if target == "users":
                conds.append(column == user_id)
            elif target == "subscriptions":
                conds.append(column.in_(sub_ids))
            elif target == "books":
                conds.append(column.in_(book_ids))
    return conds


Digest = dict[str, tuple[int, str]]


def owned_digest(engine: Engine, user_id: int) -> Digest:
    """某使用者名下每张表的行数与内容摘要（按主键排序后整行哈希）。"""
    out: Digest = {}
    with Session(engine) as s:
        subs = repo.list_subscriptions(s, user_id)
        sub_ids = [sub.id for sub in subs]
        book_ids = [b.id for sub in subs for b in repo.list_books(s, user_id, sub.id)]
        for table in Base.metadata.sorted_tables:
            conds = _owned_conditions(table, user_id, sub_ids, book_ids)
            if not conds:
                continue
            stmt = select(table).where(or_(*conds)).order_by(*table.primary_key.columns)
            rows = [tuple(row) for row in s.execute(stmt)]
            out[table.name] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
    return out


@dataclass
class Pair:
    """甲（a@example.com，已跑两晚、有成交）与乙（有订阅、信号、特征金额入金、会话、重设令牌）。"""

    app: FastAPI
    engine: Engine
    a: TestClient
    b: TestClient
    b_id: int
    a_ids: dict[IdKind, int]
    b_ids: dict[IdKind, int]
    a_void_target: int  # 甲名下一条可作废的入金，用来证明作废路由对本人可用

    def digest(self) -> Digest:
        return owned_digest(self.engine, self.b_id)


def _pending_buy(db: Session, user_id: int, sub: Subscription) -> Signal:
    book = repo.get_book_of_kind(db, user_id, sub.id, "manual")
    pending = repo.list_signals(db, user_id, book.id, statuses=("created", "notified"))
    return next(sig for sig in pending if sig.side == "buy")


def _deposit(db: Session, world: World, user_id: int, sub: Subscription, amount: float) -> int:
    today = world.clock.now.date()
    market = build_store_market(world.store, CFG, world.calendar, today)
    entry = manual_ops.record_cash(
        db,
        user_id,
        sub.id,
        market,
        world.calendar,
        CFG,
        today,
        "deposit",
        world.days[DAY + 1],
        amount,
        actor=CLI,
    )
    db.commit()
    return entry.id


@pytest.fixture
def pair(
    world: World, db: Session, db_engine: Engine, tmp_path: Path, make_user: MakeUser
) -> Iterator[Pair]:
    a = make_user("a@example.com")
    b = make_user(B_EMAIL)
    admin = make_user("boss@example.com", admin=True)
    b_sub = subscribe(db, world, b.id)  # 先建乙的订阅，两晚的每日任务也给乙出信号
    a_sub = run_two_days(world.env, db, a.id)
    b_entry = _deposit(db, world, b.id, b_sub, B_MARKER)
    a_void_target = _deposit(db, world, a.id, a_sub, 1000.0)
    invite = auth.create_invite(db, admin.id, "给乙", 14, world.clock.now)
    assert auth.request_password_reset(db, B_EMAIL, CFG.web, world.clock.now) is not None
    db.commit()
    a_entry = db.scalars(
        select(LedgerEntry).where(LedgerEntry.user_id == a.id).order_by(LedgerEntry.id)
    ).first()
    assert a_entry is not None
    a_ids: dict[IdKind, int] = {
        "subscription": a_sub.id,
        "entry": a_entry.id,
        "signal": _pending_buy(db, a.id, a_sub).id,
        "user": a.id,
        "invite": invite.id,
    }
    b_ids: dict[IdKind, int] = {
        "subscription": b_sub.id,
        "entry": b_entry,
        "signal": _pending_buy(db, b.id, b_sub).id,
        "user": b.id,
        "invite": invite.id,
    }
    app = create_app(world_deps(world, db_engine, tmp_path))
    with TestClient(app, base_url=BASE) as client_a, TestClient(app, base_url=BASE) as client_b:
        assert login_as(client_b, B_EMAIL).status_code == 303  # 乙有一个有效会话
        assert login_as(client_a, "a@example.com").status_code == 303
        yield Pair(app, db_engine, client_a, client_b, b.id, a_ids, b_ids, a_void_target)


def _send(client: TestClient, method: str, url: str) -> httpx.Response:
    if method == "GET":
        return client.get(url, follow_redirects=False)
    token = csrf_of(client.get("/settings"))
    return client.post(url, data={"csrf": token}, follow_redirects=False)


def test_digest_covers_every_user_table(pair: Pair) -> None:
    covered = set(pair.digest())
    assert covered == OWNED_TABLES
    everything = {t.name for t in Base.metadata.sorted_tables}
    assert everything - covered == SHARED_TABLES  # 新表没有使用者归属时要在这里说明
    counts = {name: count for name, (count, _) in pair.digest().items()}
    for name in (
        "users",
        "subscriptions",
        "books",
        "book_days",
        "ledger_entries",
        "signals",
        "web_sessions",
        "email_tokens",
        "outbox",
        "audit_log",
    ):
        assert counts[name] > 0, name


def test_user_cannot_reach_other_users_data_by_id(pair: Pair) -> None:
    before = pair.digest()
    for (method, path), params in USER_IDS.items():
        for name, kind in params.items():
            foreign = _url(path, {name: pair.b_ids[kind]})
            resp = _send(pair.a, method, foreign)
            assert resp.status_code == 404, (method, foreign, resp.status_code)
            assert pair.digest() == before, (method, foreign)
            # 同一条路由对本人的编号是可用的（不是对谁都 404）
            own = pair.a_void_target if method == "POST" else pair.a_ids[kind]
            own_resp = _send(pair.a, method, _url(path, {name: own}))
            assert own_resp.status_code in (200, 303), (method, path, own_resp.status_code)
            if own_resp.status_code == 303:
                assert not own_resp.headers["location"].startswith("/login"), (method, path)
    assert pair.digest() == before


def test_non_admin_cannot_use_admin_ids(pair: Pair, db: Session) -> None:
    before = pair.digest()
    invite_before = db.get(Invite, pair.b_ids["invite"])
    assert invite_before is not None and invite_before.revoked_at is None
    for (method, path), params in ADMIN_IDS.items():
        for name, kind in params.items():
            for target in (pair.b_ids[kind], pair.a_ids[kind]):
                resp = _send(pair.a, method, _url(path, {name: target}))
                assert resp.status_code == 404, (method, path, target)
                assert pair.digest() == before, (method, path, target)
    db.expire_all()
    invite_after = db.get(Invite, pair.b_ids["invite"])
    assert invite_after is not None and invite_after.revoked_at is None


def test_pages_never_show_other_users_data(pair: Pair) -> None:
    urls: list[str] = []
    for method, path in _routes("user", "GET"):
        params = USER_IDS.get((method, path), {})
        if not _path_names(path):
            urls.append(path)
        for name, kind in params.items():
            urls.append(_url(path, {name: pair.a_ids[kind]}))
    assert len(urls) >= 20
    # 对照：乙看自己的手动账页面能看到自己的邮箱和特征金额，证明下面"看不到"的检查不是空转
    own = pair.b.get("/manual")
    assert own.status_code == 200
    assert B_EMAIL in own.text
    assert any(marker in own.text for marker in B_MARKER_TEXTS)
    for url in urls:
        resp = pair.a.get(url)
        assert resp.status_code in (200, 404), (url, resp.status_code)
        if url != "/report":  # 没有回测报告文件时是 404
            assert resp.status_code == 200, url
        for secret in (B_EMAIL, *B_MARKER_TEXTS):
            assert secret not in resp.text, (url, secret)
