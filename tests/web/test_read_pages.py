"""四个只读页面（总览、信号、系统账、日志）：三种状态下的说明文字与关键数字、订阅归属、只读的历史订阅。"""

import dataclasses
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from qt.db import repo
from qt.db.models import DailyRun, User
from qt.service import accounts
from qt.service.actor import CLI
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.jobs.conftest import CASH, DAY, run_two_days
from tests.web.conftest import BASE, MakeUser, World, login_as, subscribe, world_deps

PAGES = ("/overview", "/signals", "/system", "/journal")
DISCLAIMER = "以上内容是按你设定的规则计算的结果，不构成投资建议。"
NO_SUB = "你还没有订阅策略"
NO_RUN = "系统还没有运行过，第一个交易日 18:00 之后会有结果"
DATA_FAILED = "今日数据异常，未出信号"
READ_ONLY = "正在查看已关闭的订阅（只读）"
_POST_FORM = re.compile(r'<form method="post"[^>]*>')


@pytest.fixture
def deps(world: World, db_engine: Engine, tmp_path: Path) -> WebDeps:
    return world_deps(world, db_engine, tmp_path)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _login(client: TestClient, make_user: MakeUser, email: str = "a@example.com") -> User:
    user = make_user(email)
    assert login_as(client, email).status_code == 303
    return user


def _get(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    assert DISCLAIMER in resp.text
    assert "建议买入" not in resp.text and "推荐" not in resp.text
    return resp.text


def test_pages_without_subscription(client: TestClient, make_user: MakeUser, world: World) -> None:
    _login(client, make_user)
    for path in PAGES:
        text = _get(client, path)
        assert NO_SUB in text and 'href="/strategy"' in text, path
    assert NO_RUN in _get(client, "/overview")


def test_pages_with_subscription_but_no_run(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user = _login(client, make_user)
    subscribe(db, world, user.id)
    for path in PAGES:
        text = _get(client, path)
        assert NO_RUN in text and NO_SUB not in text, path
    assert f"{CASH:,.2f}" in _get(client, "/overview")  # 手动账现金即起始资金


def test_pages_after_two_trading_days(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    system = repo.get_book_of_kind(db, user.id, sub.id, "system")
    last = repo.list_book_days(db, user.id, system.id)[-1]

    overview = _get(client, "/overview")
    assert NO_RUN not in overview and DATA_FAILED not in overview
    assert f"{last.equity:,.2f}" in overview and f"{last.cash:,.2f}" in overview
    assert str(world.days[DAY + 1]) in overview

    signals = _get(client, "/signals")
    assert "600002" in signals and "已通知" in signals and "已执行" in signals
    assert "/manual/trade?signal=" in signals  # 待处理的买入信号可以按此录入

    system_page = _get(client, "/system")
    assert "按信号买入" in system_page and f"{last.equity:,.2f}" in system_page

    journal = _get(client, "/journal")
    assert "600001" in journal and "自主卖出" in journal


def test_overview_shows_data_failure(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user = _login(client, make_user)
    subscribe(db, world, user.id)
    moment = datetime(2022, 1, 4, 10, 0, tzinfo=UTC)
    db.add(
        DailyRun(
            trade_day=world.days[DAY + 1],
            status="data_failed",
            stage="data",
            started_at=moment,
            finished_at=moment,
            detail={"error": "证券列表取不到"},
        )
    )
    db.commit()
    text = _get(client, "/overview")
    assert DATA_FAILED in text and "证券列表取不到" not in text


def test_foreign_subscription_is_404(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    other = make_user("b@example.com")
    theirs = subscribe(db, world, other.id)
    _login(client, make_user)
    for path in PAGES:
        assert client.get(f"{path}?sub={theirs.id}").status_code == 404, path
        assert client.get(f"{path}?sub=999999").status_code == 404, path


def test_own_closed_subscription_is_read_only(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    accounts.close_subscription(db, user.id, sub.id, actor=CLI)
    db.commit()
    for path in PAGES:
        text = _get(client, f"{path}?sub={sub.id}")
        assert READ_ONLY in text, path
        forms = _POST_FORM.findall(text)
        assert forms == ['<form method="post" action="/logout" class="inline">'], path
        assert "/manual/trade" not in text, path


def test_current_subscription_has_no_read_only_banner(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user = _login(client, make_user)
    sub = subscribe(db, world, user.id)
    for path in PAGES:
        assert READ_ONLY not in _get(client, f"{path}?sub={sub.id}"), path


def test_no_run_notice_uses_configured_run_time(
    deps: WebDeps, make_user: MakeUser, world: World
) -> None:
    service = deps.cfg.service.model_copy(update={"run_time": "19:45"})
    later = dataclasses.replace(deps, cfg=deps.cfg.model_copy(update={"service": service}))
    with TestClient(create_app(later), base_url=BASE) as c:
        _login(c, make_user)
        text = _get(c, "/overview")
    assert "系统还没有运行过，第一个交易日 19:45 之后会有结果" in text
    assert "18:00" not in text
