"""账号邮件：立即发送失败时留在发件箱；后台一轮只补发账号类邮件并每天清理一次过期数据。"""

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from qt.db.models import Outbox, WebSession
from qt.mail.outbox import enqueue
from qt.web.app import create_app, run_background_once
from qt.web.deps import WebDeps
from tests.web.conftest import BASE, PW, Clock, MakeUser, csrf_of, login_as


class _FailingSender:
    def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
        raise ConnectionError("模拟 SMTP 不可用")


def _statuses(engine: Engine) -> dict[str, str]:
    with Session(engine) as s:
        return {row.kind: row.status for row in s.scalars(select(Outbox))}


def test_failed_immediate_send_stays_pending_then_background_sends(
    web_deps: WebDeps,
    db_engine: Engine,
    invite_code: Callable[[], str],
) -> None:
    failing = replace(web_deps, sender=_FailingSender())
    with TestClient(create_app(failing), base_url=BASE) as c:
        csrf = csrf_of(c.get("/register"))
        resp = c.post(
            "/register",
            data={
                "email": "new@example.com",
                "password": PW,
                "invite": invite_code(),
                "accept_terms": "on",
                "csrf": csrf,
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert _statuses(db_engine) == {"account_verify": "pending"}
    run_background_once(web_deps, None)
    assert _statuses(db_engine) == {"account_verify": "sent"}


def test_background_only_sends_account_kinds_and_purges_daily(
    web_deps: WebDeps,
    db_engine: Engine,
    make_user: MakeUser,
    clock: Clock,
) -> None:
    user = make_user("foo@example.com")
    with Session(db_engine) as s:
        enqueue(s, user.id, user.email, "account_reset", "k1", "重置", "<p>x</p>", "x")
        enqueue(s, user.id, user.email, "buy_signal", "k2", "信号", "<p>y</p>", "y")
        s.commit()
    with TestClient(create_app(web_deps), base_url=BASE) as c:
        login_as(c, "foo@example.com")
    clock.advance(timedelta(days=31))

    first = run_background_once(web_deps, None)
    assert first == clock.now.date()
    assert _statuses(db_engine) == {"account_reset": "sent", "buy_signal": "pending"}
    with Session(db_engine) as s:
        assert s.scalar(select(func.count()).select_from(WebSession)) == 0
    # 同一天不再清理
    assert run_background_once(web_deps, first) == first


def test_background_thread_starts_and_stops(web_deps: WebDeps) -> None:
    deps = replace(web_deps, background=True)
    with TestClient(create_app(deps), base_url=BASE):
        names = {t.name for t in threading.enumerate()}
        assert "qt-web-background" in names
    assert "qt-web-background" not in {t.name for t in threading.enumerate()}
