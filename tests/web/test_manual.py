"""手动账页面：查看、录入买入卖出、已有持仓、入金出金、作废；失败原因、归属隔离、账本不一致。"""

import dataclasses
import re
from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import qt.admin_cli
import qt.cli
from qt.db import repo
from qt.db.models import LedgerEntry, Signal, Subscription
from qt.service import accounts, views
from qt.service.actor import CLI
from qt.service.form_keys import new_form_value
from qt.service.market import StoreMarket, build_store_market
from qt.web.app import create_app
from qt.web.deps import WebDeps
from tests.conftest import resolve_test_db_url
from tests.jobs.conftest import CASH, CFG, DAY, run_two_days
from tests.web.conftest import (
    BASE,
    MakeUser,
    World,
    csrf_of,
    login_as,
    subscribe,
    world_deps,
)
from tests.web.test_read_pages import DISCLAIMER

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "a@example.com"


@pytest.fixture
def deps(world: World, db_engine: Engine, tmp_path: Path) -> WebDeps:
    return world_deps(world, db_engine, tmp_path)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _login(client: TestClient, make_user: MakeUser, email: str = EMAIL) -> int:
    user = make_user(email)
    assert login_as(client, email).status_code == 303
    return user.id


def _post(client: TestClient, path: str, data: dict[str, str]) -> httpx.Response:
    """带防伪造令牌提交表单；没给 form_key 的（录入表单）每次用一个新的一次性值。"""
    token = csrf_of(client.get("/manual/cash"))
    extra = {} if "/void" in path or "form_key" in data else {"form_key": new_form_value()}
    return client.post(path, data={**data, **extra, "csrf": token}, follow_redirects=False)


def _form_key(page: str) -> str:
    found = re.search(r'name="form_key" value="([^"]+)"', page)
    assert found is not None, "页面里没有 form_key"
    return found.group(1)


def _ledger(db: Session, sub: Subscription) -> list[LedgerEntry]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    return repo.list_ledger_entries(db, sub.user_id, book.id)


def _buy_form(world: World, **over: str) -> dict[str, str]:
    form = {
        "side": "buy",
        "symbol": "600001",
        "day": str(world.days[DAY]),
        "price": "10.00",
        "shares": "1000",
        "fees": "",
    }
    return {**form, **over}


def _page(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    assert DISCLAIMER in resp.text
    return resp.text


# ---- 成功路径 ----


def test_buy_then_sell(client: TestClient, make_user: MakeUser, world: World, db: Session) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(client, "/manual/trade", _buy_form(world))
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/manual?msg=manual_trade&entry=")
    rows = _ledger(db, sub)
    assert [(r.kind, r.symbol, r.shares, r.price) for r in rows] == [("buy", "600001", 1000, 10.0)]
    assert rows[0].fees_estimated and rows[0].fees > 0 and rows[0].origin == "self"
    page = _page(client, resp.headers["location"])
    assert "已录入成交" in page and "买入 600001" in page and "1,000 股" in page
    assert "（估算）" in page and "作废" in page

    sold = _post(
        client,
        "/manual/trade",
        _buy_form(world, side="sell", day=str(world.days[DAY + 1]), price="10.50", shares="400"),
    )
    assert sold.status_code == 303
    assert [r.kind for r in _ledger(db, sub)] == ["buy", "sell"]
    assert "卖出 600001" in _page(client, sold.headers["location"])
    held = views.manual_book(
        db,
        sub.user_id,
        sub.id,
        _market(world),
        world.calendar,
        CFG,
        world.clock.now.date(),
    )
    assert [(h.symbol, h.shares) for h in held.holdings] == [("600001", 600)]


def _market(world: World) -> StoreMarket:
    return build_store_market(world.store, CFG, world.calendar, world.clock.now.date())


def test_explicit_fees_are_kept(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    assert _post(client, "/manual/trade", _buy_form(world, fees="7.5")).status_code == 303
    (row,) = _ledger(db, sub)
    assert row.fees == 7.5 and not row.fees_estimated


def test_record_holding(client: TestClient, make_user: MakeUser, world: World, db: Session) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(
        client,
        "/manual/holding",
        {
            "symbol": "600003",
            "buy_day": str(world.days[DAY - 30]),
            "cost_price": "9.50",
            "shares": "2000",
        },
    )
    assert resp.status_code == 303
    (row,) = _ledger(db, sub)
    assert (row.kind, row.symbol, row.shares, row.price) == ("transfer_in", "600003", 2000, 9.5)
    page = _page(client, resp.headers["location"])
    assert "已录入已有持仓" in page and "600003" in page and "自主买入" in page


def test_deposit_and_withdraw(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    day = str(world.days[DAY])
    dep = _post(client, "/manual/cash", {"kind": "deposit", "day": day, "amount": "5000"})
    assert dep.status_code == 303 and "已录入入金" in _page(client, dep.headers["location"])
    wd = _post(client, "/manual/cash", {"kind": "withdraw", "day": day, "amount": "2000"})
    assert wd.status_code == 303 and "已录入出金" in _page(client, wd.headers["location"])
    assert [(r.kind, r.price) for r in _ledger(db, sub)] == [("deposit", 5000), ("withdraw", 2000)]
    assert f"{CASH + 3000:,.2f}" in _page(client, "/manual")


def test_void_entry_with_confirmation(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world))
    (buy,) = _ledger(db, sub)
    manual = _page(client, "/manual")
    assert f"/manual/entries/{buy.id}/void" in manual

    confirm = _page(client, f"/manual/entries/{buy.id}/void")
    assert "确认作废" in confirm and "买入 600001" in confirm
    assert len(_ledger(db, sub)) == 1  # 确认页不改数据

    resp = _post(client, f"/manual/entries/{buy.id}/void", {})
    assert resp.status_code == 303
    rows = _ledger(db, sub)
    assert [r.kind for r in rows] == ["buy", "void"] and rows[1].voids_entry_id == buy.id
    after = _page(client, resp.headers["location"])
    assert "已作废流水" in after and "（已作废）" in after
    assert f"/manual/entries/{buy.id}/void" not in after
    assert f"{CASH:,.2f}" in after  # 现金回到起始资金

    again = client.get(f"/manual/entries/{buy.id}/void", follow_redirects=False)
    assert again.status_code == 303 and "void_na" in again.headers["location"]
    assert "不能作废" in _page(client, again.headers["location"])
    twice = _post(client, f"/manual/entries/{buy.id}/void", {})
    assert twice.status_code == 422 and len(_ledger(db, sub)) == 2


# ---- 失败路径：422，页面带服务给出的原因，保留输入 ----


def _failed(resp: httpx.Response, reason: str, *kept: str) -> None:
    assert resp.status_code == 422, resp.text[:300]
    assert reason in resp.text
    for value in kept:
        assert f'value="{value}"' in resp.text


def test_buy_non_main_board_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(client, "/manual/trade", _buy_form(world, symbol="300001"))
    _failed(resp, "主板", "300001", "10.00", "1000")
    assert _ledger(db, sub) == []
    junk = _post(client, "/manual/trade", _buy_form(world, symbol="abc"))
    assert junk.status_code == 422 and _ledger(db, sub) == []


def test_buy_future_date_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    future = str(world.days[DAY + 5])
    resp = _post(client, "/manual/trade", _buy_form(world, day=future))
    _failed(resp, "晚于今天", future)
    assert _ledger(db, sub) == []


def test_sell_more_than_held_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world, shares="100"))
    resp = _post(
        client,
        "/manual/trade",
        _buy_form(world, side="sell", day=str(world.days[DAY + 1]), shares="500"),
    )
    _failed(resp, "超过", "500")
    assert len(_ledger(db, sub)) == 1


def test_withdraw_more_than_cash_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    amount = f"{CASH * 10:.0f}"
    resp = _post(
        client, "/manual/cash", {"kind": "withdraw", "day": str(world.days[DAY]), "amount": amount}
    )
    _failed(resp, "现金", amount)
    assert _ledger(db, sub) == []


def test_holding_future_buy_day_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(
        client,
        "/manual/holding",
        {
            "symbol": "600003",
            "buy_day": str(world.days[DAY + 5]),
            "cost_price": "9",
            "shares": "100",
        },
    )
    assert resp.status_code == 422 and "晚于今天" in resp.text and _ledger(db, sub) == []


def test_bad_format_shows_field_errors_and_keeps_inputs(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(
        client,
        "/manual/trade",
        _buy_form(world, symbol=" ", price="abc", shares="1e3", day="2022/01/03", fees="x"),
    )
    assert resp.status_code == 422
    for hint in ("请填写股票代码", "金额应为数字", "数量应为数字", "日期格式应为", "金额应为数字"):
        assert hint in resp.text
    assert 'value="abc"' in resp.text and 'value="1e3"' in resp.text
    bad_side = _post(client, "/manual/trade", _buy_form(world, side="short"))
    assert bad_side.status_code == 422 and "请选择买入或卖出" in bad_side.text
    bad_kind = _post(client, "/manual/cash", {"kind": "x", "day": "2022-01-03", "amount": "1"})
    assert bad_kind.status_code == 422 and "请选择入金或出金" in bad_kind.text
    assert _ledger(db, sub) == []


def test_pages_say_main_board_only(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    subscribe(db, world, _login(client, make_user))
    assert "目前只支持沪深主板股票" in _page(client, "/manual/trade")
    assert "目前只支持沪深主板股票" in _page(client, "/manual/holding")
    assert "目前只支持沪深主板股票" in _page(client, "/manual")


def test_same_form_submitted_twice_writes_one_entry(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    """同一个表单（同一个一次性值）连点两次：只有一条流水，第二次 303 到 /manual 并提示已录入过。"""
    sub = subscribe(db, world, _login(client, make_user))
    form = {**_buy_form(world), "form_key": _form_key(_page(client, "/manual/trade"))}
    first = _post(client, "/manual/trade", form)
    second = _post(client, "/manual/trade", form)
    assert first.headers["location"].startswith("/manual?msg=manual_trade")
    assert (second.status_code, second.headers["location"]) == (303, "/manual?msg=manual_duplicate")
    assert "这笔已经录入过，没有重复记录" in _page(client, second.headers["location"])
    assert [(r.kind, r.shares) for r in _ledger(db, sub)] == [("buy", 1000)]


def test_holding_and_cash_forms_are_also_deduplicated(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    holding = {
        "symbol": "600003",
        "buy_day": str(world.days[DAY - 30]),
        "cost_price": "9",
        "shares": "100",
        "form_key": new_form_value(),
    }
    cash = {
        "kind": "deposit",
        "day": str(world.days[DAY]),
        "amount": "100",
        "form_key": new_form_value(),
    }
    for path, data in (("/manual/holding", holding), ("/manual/cash", cash)):
        assert _post(client, path, data).headers["location"].startswith("/manual?msg=manual_")
        again = _post(client, path, data)
        assert again.headers["location"] == "/manual?msg=manual_duplicate"
    assert [r.kind for r in _ledger(db, sub)] == ["transfer_in", "deposit"]


def test_two_renders_with_same_content_are_two_trades(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    first_key = _form_key(_page(client, "/manual/trade"))
    second_key = _form_key(_page(client, "/manual/trade"))
    assert first_key != second_key
    for key in (first_key, second_key):
        assert (
            _post(client, "/manual/trade", {**_buy_form(world), "form_key": key}).status_code == 303
        )
    assert len(_ledger(db, sub)) == 2


def test_failed_write_does_not_burn_the_form_value(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    key = new_form_value()
    bad = _post(client, "/manual/trade", {**_buy_form(world, shares="150"), "form_key": key})
    assert bad.status_code == 422 and _ledger(db, sub) == []
    fresh = _form_key(bad.text)
    assert fresh != key  # 重新显示的表单带新的值
    # 用重新显示的表单改正后提交成功；原来的值也没有被占用
    assert (
        _post(client, "/manual/trade", {**_buy_form(world), "form_key": fresh}).status_code == 303
    )
    assert _post(client, "/manual/trade", {**_buy_form(world), "form_key": key}).status_code == 303
    assert len(_ledger(db, sub)) == 2


@pytest.mark.parametrize("value", ["", "short", "has space and ?? characters!!", "x" * 65])
def test_missing_or_malformed_form_value_is_rejected(
    client: TestClient, make_user: MakeUser, world: World, db: Session, value: str
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    token = csrf_of(client.get("/manual/cash"))
    paths = {
        "/manual/trade": _buy_form(world),
        "/manual/cash": {"kind": "deposit", "day": str(world.days[DAY]), "amount": "1"},
    }
    for path, data in paths.items():
        resp = client.post(
            path, data={**data, "form_key": value, "csrf": token}, follow_redirects=False
        )
        assert resp.status_code == 422 and "表单已失效" in resp.text
    missing = client.post(
        "/manual/trade", data={**_buy_form(world), "csrf": token}, follow_redirects=False
    )
    assert missing.status_code == 422
    assert _ledger(db, sub) == []


def test_forms_carry_a_form_value(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    subscribe(db, world, _login(client, make_user))
    for path in ("/manual/trade", "/manual/holding", "/manual/cash"):
        assert len(_form_key(_page(client, path))) >= 16


def test_void_twice_is_a_clear_422_with_one_void(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world))
    (buy,) = _ledger(db, sub)
    assert _post(client, f"/manual/entries/{buy.id}/void", {}).status_code == 303
    again = _post(client, f"/manual/entries/{buy.id}/void", {})
    assert again.status_code == 422 and "已作废，不能重复作废" in again.text
    assert [r.kind for r in _ledger(db, sub)] == ["buy", "void"]


def test_unsupported_symbol_does_not_build_a_market(
    deps: WebDeps, make_user: MakeUser, world: World, db: Session
) -> None:
    builds: list[str] = []

    def counting(today: date, symbols: Collection[str]) -> StoreMarket:
        builds.append(",".join(sorted(symbols)))
        return deps.market(today, symbols)

    with TestClient(create_app(dataclasses.replace(deps, market=counting)), base_url=BASE) as c:
        sub = subscribe(db, world, _login(c, make_user))
        before = len(builds)
        for symbol in ("abc", "300001", "9" * 500):
            resp = _post(c, "/manual/trade", _buy_form(world, symbol=symbol))
            assert resp.status_code == 422 and "股票代码不存在" in resp.text
            held = _post(
                c,
                "/manual/holding",
                {
                    "symbol": symbol,
                    "buy_day": str(world.days[DAY - 30]),
                    "cost_price": "9",
                    "shares": "100",
                },
            )
            assert held.status_code == 422 and "股票代码不存在" in held.text
        # 每次提交页面都会 GET 一下 /manual/cash 取令牌，那不建行情；提交本身一次也没建
        assert len(builds) == before
        assert _ledger(db, sub) == []
        assert _post(c, "/manual/trade", _buy_form(world)).status_code == 303
        assert len(builds) > before  # 合法代码照常建行情


# ---- 没有订阅 ----


def test_without_open_subscription_everything_redirects_to_strategy(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    user_id = _login(client, make_user)
    sub = subscribe(db, world, user_id)
    accounts.close_subscription(db, user_id, sub.id, actor=CLI)
    db.commit()
    for path in ("/manual", "/manual/trade", "/manual/holding", "/manual/cash"):
        resp = client.get(path, follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/strategy"), path
    posts = {
        "/manual/trade": _buy_form(world),
        "/manual/holding": {
            "symbol": "600003",
            "buy_day": "2022-01-03",
            "cost_price": "9",
            "shares": "1",
        },
        "/manual/cash": {"kind": "deposit", "day": "2022-01-03", "amount": "1"},
        "/manual/entries/1/void": {},
    }
    for path, data in posts.items():
        resp = _post(client, path, data)
        assert (resp.status_code, resp.headers["location"]) == (303, "/strategy"), path
    assert _ledger(db, sub) == []


# ---- 归属隔离 ----


def _other_client(deps: WebDeps, make_user: MakeUser) -> tuple[TestClient, int]:
    user = make_user("b@example.com")
    other = TestClient(create_app(deps), base_url=BASE)
    assert login_as(other, "b@example.com").status_code == 303
    return other, user.id


def test_signal_prefill_and_isolation(
    client: TestClient, deps: WebDeps, make_user: MakeUser, world: World, db: Session
) -> None:
    user_id = _login(client, make_user)
    sub = run_two_days(world.env, db, user_id)
    book = repo.get_book_of_kind(db, user_id, sub.id, "manual")
    pending = [
        sig
        for sig in repo.list_signals(db, user_id, book.id, statuses=("created", "notified"))
        if sig.side == "buy"
    ]
    assert pending
    sig = pending[0]
    page = _page(client, f"/manual/trade?signal={sig.id}")
    assert f'value="{sig.symbol}"' in page and f'value="{sig.shares:.0f}"' in page
    assert re.search(r'<option value="buy" selected>', page)
    assert f"信号 #{sig.id}" in page

    # 本人已处理过的信号（如已执行）不能预填：回到信号页并说明，而不是 404
    done = db.scalars(select(Signal).where(Signal.user_id == user_id, Signal.status == "executed"))
    executed = next(iter(done))
    resp = client.get(f"/manual/trade?signal={executed.id}", follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "/signals?msg=signal_handled")
    assert "这条信号已经处理过或已过期，可以在下面查看它的状态" in _page(
        client, resp.headers["location"]
    )
    # 不存在的编号仍是 404
    assert client.get("/manual/trade?signal=999999").status_code == 404

    other, other_id = _other_client(deps, make_user)
    with other:
        # 别人的信号，无论能不能录入、自己有没有订阅，都是 404（不暴露别人的信号是否存在）
        assert other.get(f"/manual/trade?signal={sig.id}").status_code == 404
        assert other.get(f"/manual/trade?signal={executed.id}").status_code == 404
        subscribe(db, world, other_id)
        assert other.get(f"/manual/trade?signal={sig.id}").status_code == 404
        assert other.get(f"/manual/trade?signal={executed.id}").status_code == 404


def test_cannot_void_someone_elses_entry(
    client: TestClient, deps: WebDeps, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world))
    (mine,) = _ledger(db, sub)
    other, other_id = _other_client(deps, make_user)
    with other:
        other_sub = subscribe(db, world, other_id)
        assert other.get(f"/manual/entries/{mine.id}/void").status_code == 404
        token = csrf_of(other.get("/manual/cash"))
        resp = other.post(
            f"/manual/entries/{mine.id}/void", data={"csrf": token}, follow_redirects=False
        )
        assert resp.status_code == 404
        assert other.get("/manual/entries/999999/void").status_code == 404
        # 对方录入的流水不带到我的账里，我的账页面也看不到对方的
        _post_other = other.post(
            "/manual/trade",
            data={
                **_buy_form(world, symbol="600002"),
                "form_key": new_form_value(),
                "csrf": token,
            },
            follow_redirects=False,
        )
        assert _post_other.status_code == 303
    assert [(r.kind, r.symbol) for r in _ledger(db, sub)] == [("buy", "600001")]
    assert [(r.kind, r.symbol) for r in _ledger(db, other_sub)] == [("buy", "600002")]
    assert "600002" not in _page(client, "/manual")


def test_manual_page_with_someone_elses_entry_is_404(
    client: TestClient, deps: WebDeps, make_user: MakeUser, world: World, db: Session
) -> None:
    """网址里的流水编号只按本人名下查找：别人的、不存在的一律 404，不当作没给。"""
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world))
    (mine,) = _ledger(db, sub)
    assert "买入 600001" in _page(client, f"/manual?msg=manual_trade&entry={mine.id}")
    other, other_id = _other_client(deps, make_user)
    with other:
        subscribe(db, world, other_id)
        assert other.get(f"/manual?msg=manual_trade&entry={mine.id}").status_code == 404
        assert other.get(f"/manual?entry={mine.id}").status_code == 404
        assert other.get("/manual?msg=manual_trade&entry=999999").status_code == 404
        assert other.get("/manual").status_code == 200


def test_unauthenticated_access_redirects_to_login(client: TestClient) -> None:
    for path in ("/manual", "/manual/trade", "/manual/holding", "/manual/cash"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


# ---- 账本不一致（R7） ----


def _break_ledger(db: Session, sub: Subscription, world: World) -> LedgerEntry:
    """绕过校验写一笔超过现金的出金：模拟数据源修订分红后现有流水已不一致。"""
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    row = LedgerEntry(
        book_id=book.id,
        user_id=sub.user_id,
        kind="withdraw",
        trade_day=world.days[DAY],
        effective_day=world.days[DAY],
        shares=0.0,
        price=CASH * 3,
        fees=0.0,
        fees_estimated=False,
        created_by_user_id=sub.user_id,
    )
    db.add(row)
    db.commit()
    return row


def test_inconsistent_ledger_pages_do_not_fail(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    bad = _break_ledger(db, sub, world)
    manual = _page(client, "/manual")
    assert "互相对不上" in manual and "超过" in manual
    assert f"/manual/entries/{bad.id}/void" in manual and "出金" in manual
    overview = _page(client, "/overview")
    assert "互相对不上" in overview and "超过" in overview
    signals = _page(client, "/signals")
    assert "互相对不上" in signals

    # 作废这条出金即可修复：作废页照常打开，作废后恢复正常
    assert "确认作废" in _page(client, f"/manual/entries/{bad.id}/void")
    resp = _post(client, f"/manual/entries/{bad.id}/void", {})
    assert resp.status_code == 303
    fixed = _page(client, resp.headers["location"])
    assert "互相对不上" not in fixed and f"{CASH:,.2f}" in fixed


def test_unrelated_write_on_inconsistent_ledger_is_422_not_500(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _break_ledger(db, sub, world)
    resp = _post(client, "/manual/trade", _buy_form(world))
    assert resp.status_code == 422 and "现有流水" in resp.text
    assert "qt admin" not in resp.text and "作废有问题的记录" in resp.text
    assert len(_ledger(db, sub)) == 1


# ---- 与命令行一致 ----


def test_web_entry_matches_cli_book_show(
    client: TestClient,
    make_user: MakeUser,
    world: World,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    _post(client, "/manual/trade", _buy_form(world))
    _post(
        client,
        "/manual/holding",
        {
            "symbol": "600003",
            "buy_day": str(world.days[DAY - 30]),
            "cost_price": "9",
            "shares": "300",
        },
    )
    deposit = {"kind": "deposit", "day": str(world.days[DAY]), "amount": "5000"}
    _post(client, "/manual/cash", deposit)

    monkeypatch.setenv("QT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QT_CONFIG", str(ROOT / "config" / "default.yaml"))
    monkeypatch.setenv("DATABASE_URL", resolve_test_db_url())
    monkeypatch.setattr(qt.admin_cli, "now", lambda: world.clock.now)
    result = CliRunner().invoke(qt.cli.app, ["admin", "book", "show", "--email", EMAIL])
    assert result.exit_code == 0, result.output

    db.rollback()
    view = views.manual_book(
        db, sub.user_id, sub.id, _market(world), world.calendar, CFG, world.clock.now.date()
    )
    web = _page(client, "/manual")
    cash = f"{view.summary.cash:,.2f}"
    assert f"现金 {cash}" in result.output and cash in web
    assert len(view.holdings) == 2
    for holding in view.holdings:
        assert holding.symbol in result.output and holding.symbol in web
        assert f"{holding.shares:,.0f}" in result.output and f"{holding.shares:,.0f}" in web
    count = db.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 3


def test_nul_byte_in_symbol_is_400_and_records_nothing(
    client: TestClient, make_user: MakeUser, world: World, db: Session
) -> None:
    sub = subscribe(db, world, _login(client, make_user))
    resp = _post(client, "/manual/trade", _buy_form(world, symbol="600\x00001"))
    assert resp.status_code == 400
    assert "提交的内容无法识别" in resp.text
    assert _ledger(db, sub) == []
