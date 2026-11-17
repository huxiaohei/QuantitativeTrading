"""持仓表的盘中价：交易时段内带刷新地址并按报价重算，其余时间与原来一样；片段路由的归属与出错情形。"""

from collections.abc import Collection, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from qt.data.quotes import LiveQuote, QuoteCache
from qt.db import repo
from qt.db.models import Subscription, User
from qt.service import views
from qt.service.market import build_store_market
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.jobs.conftest import DAY, run_two_days
from tests.web.conftest import BASE, CFG, MakeUser, World, login_as, subscribe, world_deps

LIVE_URL = 'data-live-url="/system/holdings"'
QUOTE_AT = datetime(2026, 1, 1, 10, 0, 3)


class FakeQuotes:
    """假的报价来源：每只股票给 price（缺省 123.45）；fail 为真时抛异常。"""

    def __init__(self) -> None:
        self.calls: list[set[str]] = []
        self.prices: dict[str, float] = {}
        self.missing: set[str] = set()
        self.fail = False

    def fetch(self, symbols: Collection[str]) -> dict[str, LiveQuote]:
        self.calls.append(set(symbols))
        if self.fail:
            raise TimeoutError("timed out")
        found = [symbol for symbol in symbols if symbol not in self.missing]
        return {s: LiveQuote(self.prices.get(s, 123.45), 100.0, QUOTE_AT) for s in found}


@pytest.fixture
def quotes() -> FakeQuotes:
    return FakeQuotes()


@pytest.fixture
def deps(world: World, db_engine: Engine, tmp_path: Path, quotes: FakeQuotes) -> WebDeps:
    return world_deps(world, db_engine, tmp_path, quotes=QuoteCache(quotes.fetch).get)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _login(client: TestClient, make_user: MakeUser, email: str = "a@example.com") -> User:
    user = make_user(email)
    assert login_as(client, email).status_code == 303
    return user


def _at(world: World, hour: int, minute: int) -> None:
    """把网页的时钟拨到最后一个已运行交易日的某个时刻。"""
    world.clock.now = world.clock.now.replace(hour=hour, minute=minute, second=0)


def _held(world: World, db: Session, user: User, sub: Subscription) -> list[views.HoldingRow]:
    symbols = views.book_symbols(db, user.id, sub.id)
    market = build_store_market(world.store, CFG, world.calendar, world.days[DAY + 1], symbols)
    return views.system_book(db, user.id, sub.id, market).holdings


def _get(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.text


def test_in_session_the_table_uses_live_prices_and_carries_the_refresh_url(
    client: TestClient, make_user: MakeUser, world: World, db: Session, quotes: FakeQuotes
) -> None:
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    held = _held(world, db, user, sub)
    assert held, "夹具应当让系统账有持仓"
    _at(world, 10, 0)

    page = _get(client, "/system")
    assert LIVE_URL in page and 'data-live-seconds="15"' in page
    assert '<script src="/static/live.js" defer></script>' in page
    assert "123.45" in page
    assert "现价为盘中价，报价时间 10:00:03" in page
    assert "信号仍按每晚收盘价计算" in page
    assert "建议买入" not in page and "推荐" not in page
    row = held[0]
    assert f"{row.shares * 123.45:,.2f}" in page  # 市值按盘中价重算
    assert quotes.calls == [{h.symbol for h in held}]

    fragment = _get(client, "/system/holdings")
    assert "<html" not in fragment and fragment.lstrip().startswith("<div")
    assert LIVE_URL in fragment and "123.45" in fragment
    assert len(quotes.calls) == 1  # 10 秒内共用缓存，不再请求


@pytest.mark.parametrize(("hour", "minute"), [(9, 29), (11, 31), (12, 59), (15, 1), (18, 30)])
def test_outside_session_nothing_changes_and_no_quote_is_requested(
    client: TestClient,
    make_user: MakeUser,
    world: World,
    db: Session,
    quotes: FakeQuotes,
    hour: int,
    minute: int,
) -> None:
    user = _login(client, make_user)
    run_two_days(world.env, db, user.id)
    _at(world, hour, minute)
    for path in ("/system", "/system/holdings"):
        text = _get(client, path)
        assert "data-live-holdings" in text
        assert "data-live-url" not in text and "盘中价" not in text and "123.45" not in text
    assert quotes.calls == []


def test_quote_failure_falls_back_to_close_and_page_still_works(
    client: TestClient, make_user: MakeUser, world: World, db: Session, quotes: FakeQuotes
) -> None:
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    held = _held(world, db, user, sub)
    quotes.fail = True
    _at(world, 10, 0)
    for path in ("/system", "/system/holdings"):
        text = _get(client, path)
        assert LIVE_URL in text  # 仍在交易时段：继续定时重试
        assert "123.45" not in text and "现价为盘中价" not in text
        assert f"{len(held)} 只暂时取不到盘中价，显示的是收盘价。" in text


def test_suspended_and_missing_symbols_are_labelled(
    client: TestClient, make_user: MakeUser, world: World, db: Session, quotes: FakeQuotes
) -> None:
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    held = _held(world, db, user, sub)
    quotes.prices = {held[0].symbol: 0.0}
    _at(world, 13, 0)
    text = _get(client, "/system/holdings")
    assert '100.00 <span class="muted">停牌</span>' in text

    quotes.missing = {h.symbol for h in held}
    world.clock.advance(world.clock.now.replace(second=30) - world.clock.now)
    text = _get(client, "/system/holdings")
    # 来源这次没给：继续用上一次的报价
    assert "停牌" in text and "暂时取不到" not in text


def test_manual_page_and_fragment(
    client: TestClient, make_user: MakeUser, world: World, db: Session, quotes: FakeQuotes
) -> None:
    user = _login(client, make_user)
    run_two_days(world.env, db, user.id)
    _at(world, 10, 0)
    page = _get(client, "/manual")
    fragment = _get(client, "/manual/holdings")
    assert "<html" not in fragment
    assert "没有持仓。" not in fragment, "夹具应当让手动账有持仓"
    for text in (page, fragment):
        assert 'data-live-url="/manual/holdings"' in text and "123.45" in text
    assert "止损位" in fragment  # 手动账的持仓表带止损位、止盈位两列
    assert quotes.calls and len(quotes.calls) == 1
    del user


def test_fragment_without_subscription_or_holdings_is_empty_and_does_not_poll(
    client: TestClient, make_user: MakeUser, world: World, db: Session, quotes: FakeQuotes
) -> None:
    user = _login(client, make_user)
    _at(world, 10, 0)
    for path in ("/manual/holdings", "/system/holdings"):
        text = _get(client, path)
        assert "data-live-holdings" in text and "data-live-url" not in text
    subscribe(db, world, user.id)
    for path in ("/manual/holdings", "/system/holdings"):
        assert "data-live-url" not in _get(client, path)
    assert quotes.calls == []


def test_foreign_subscription_is_404_and_url_keeps_own_sub(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    other = make_user("b@example.com")
    foreign = subscribe(db, world, other.id)
    user = _login(client, make_user)
    sub = run_two_days(world.env, db, user.id)
    _at(world, 10, 0)
    assert client.get(f"/system/holdings?sub={foreign.id}").status_code == 404
    own = _get(client, f"/system?sub={sub.id}")
    assert f'data-live-url="/system/holdings?sub={sub.id}"' in own
    assert repo.get_subscription(db, user.id, sub.id).id == sub.id


def test_fragments_require_login(client: TestClient) -> None:
    for path in ("/manual/holdings", "/system/holdings"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
