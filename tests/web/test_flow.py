"""贯穿流程（设计第 7 节）：邀请 → 注册 → 验证 → 登录 → 订阅 → 已有持仓 → 每日任务出信号
→ 从信号邮件打开录入页 → 提交成交 → 手动账显示新持仓 → 命令行看到的现金与持仓和页面一致。

全程只通过 HTTP 客户端和 2A 的每日任务夹具，不直接写数据库。
"""

import re
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from typer.testing import CliRunner

import qt.admin_cli
import qt.cli
from qt.jobs.daily import RunContext, run_day
from qt.mail.send import FileSender
from qt.service.market import build_store_market
from qt.web.app import create_app
from tests.conftest import resolve_test_db_url
from tests.jobs.conftest import ADMIN, CFG, DAY, evening, mirror_source
from tests.web.conftest import (
    BASE,
    PW,
    MakeUser,
    World,
    csrf_of,
    link_path,
    login_as,
    mail_text,
    read_mails,
    world_deps,
)
from tests.web.test_read_pages import DISCLAIMER

ROOT = Path(__file__).resolve().parents[2]
NEWCOMER = "newcomer@example.com"
_INVITE_LINK = re.compile(re.escape(BASE) + r"(/register\?invite=[A-Za-z0-9_-]+)")
_HIDDEN = re.compile(r'<input type="hidden" name="(token|form_key)" value="([^"]+)"')
_INPUT = re.compile(r'<input type="text" name="(symbol|shares)" value="([^"]*)"')
_CLI_CASH = re.compile(r"现金 ([\d,]+\.\d\d)")
_CLI_HOLDING = re.compile(r"^\s*(\d{6})\s+([\d,]+)\s", re.MULTILINE)


def _hidden(page: str, name: str) -> str:
    values = {k: v for k, v in _HIDDEN.findall(page)}
    assert name in values, f"页面里没有隐藏字段 {name}"
    return values[name]


def _ok(client: TestClient, path: str) -> httpx.Response:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp


def test_invite_to_recorded_trade(
    world: World,
    db_engine: Engine,
    tmp_path: Path,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = world.days
    mail_dir = tmp_path / "mails"
    clock = world.clock
    clock.now = evening(days[DAY - 1])  # 订阅起始日缺省为下一个交易日 days[DAY]
    make_user(ADMIN, admin=True)
    app = create_app(world_deps(world, db_engine, tmp_path))

    # 管理员登录，生成邀请码，从邀请码列表取注册链接
    with TestClient(app, base_url=BASE) as admin:
        assert login_as(admin, ADMIN).status_code == 303
        token = csrf_of(admin.get("/admin/invites"))
        created = admin.post(
            "/admin/invites", data={"note": "新人", "csrf": token}, follow_redirects=False
        )
        assert created.status_code == 303
        found = _INVITE_LINK.search(_ok(admin, created.headers["location"]).text)
        assert found is not None, "邀请码列表里没有注册链接"
        register_path = found.group(1)
    invite = register_path.split("=", 1)[1]

    with TestClient(app, base_url=BASE) as me:
        # 用注册链接注册（注册时同意条款），从邮件取验证链接并验证
        page = _ok(me, register_path)
        assert f'value="{invite}"' in page.text
        resp = me.post(
            "/register",
            data={
                "email": NEWCOMER,
                "password": PW,
                "invite": invite,
                "accept_terms": "on",
                "csrf": csrf_of(page),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/register/sent"
        (verify_mail,) = read_mails(mail_dir, NEWCOMER)
        verify_page = _ok(me, link_path(mail_text(verify_mail), "/verify"))
        resp = me.post(
            "/verify",
            data={"token": _hidden(verify_page.text, "token"), "csrf": csrf_of(verify_page)},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/login?msg=verified"

        # 登录：注册时已同意条款，直接进入总览
        assert login_as(me, NEWCOMER).status_code == 303
        overview = me.get("/overview", follow_redirects=False)
        assert overview.status_code == 200 and DISCLAIMER in overview.text

        # 创建订阅（起始日缺省）
        token = csrf_of(overview)
        resp = me.post(
            "/strategy/new",
            data={"initial_cash": "100000", "start_day": "", "csrf": token},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/strategy?msg=sub_created"
        assert str(days[DAY]) in _ok(me, "/strategy").text

        # 起始日当天上午录入一笔已有持仓
        clock.now = evening(days[DAY], 9, 30)
        holding_page = _ok(me, "/manual/holding")
        resp = me.post(
            "/manual/holding",
            data={
                "symbol": "600003",
                "buy_day": str(days[DAY - 30]),
                "cost_price": "9",
                "shares": "300",
                "form_key": _hidden(holding_page.text, "form_key"),
                "csrf": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"].startswith("/manual?msg=manual_holding&entry=")

        # 用 2A 的夹具跑一个交易日：信号邮件带去网页的链接，地址与网页一致
        clock.now = evening(days[DAY])
        ctx = RunContext(
            engine=db_engine,
            store=world.store,
            source=mirror_source(world.store),
            cfg=CFG,
            sender=FileSender(mail_dir),
            admin_email=ADMIN,
            now=clock,
            base_url=BASE,
        )
        assert run_day(ctx, days[DAY], notify=True, update_data=False) == "done"
        assert "/manual/trade?signal=" in _ok(me, "/signals").text
        signal_mail = mail_text(read_mails(mail_dir, NEWCOMER)[-1])
        assert "去网页录入" in signal_mail
        trade_path = link_path(signal_mail, "/manual/trade?signal=")

        # 第二天收盘后从邮件链接打开录入页：已按信号预填，按当天价格提交成交
        trade_day = days[DAY + 1]
        clock.now = evening(trade_day, 15, 30)
        trade_page = _ok(me, trade_path).text
        signal_id = trade_path.rsplit("=", 1)[1]
        assert f"信号 #{signal_id}" in trade_page
        prefilled = dict(_INPUT.findall(trade_page))
        symbol, shares = prefilled["symbol"], prefilled["shares"]
        assert symbol != "600003" and int(shares) > 0
        market = build_store_market(world.store, CFG, world.calendar, trade_day)
        span = market.price_range(trade_day, symbol)
        assert span is not None
        price = f"{(span[0] + span[1]) / 2:.2f}"
        resp = me.post(
            "/manual/trade",
            data={
                "side": "buy",
                "symbol": symbol,
                "day": str(trade_day),
                "price": price,
                "shares": shares,
                "fees": "",
                "form_key": _hidden(trade_page, "form_key"),
                "csrf": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"].startswith("/manual?msg=manual_trade&entry=")
        assert "已录入成交" in _ok(me, resp.headers["location"]).text

        # 手动账页显示新持仓
        manual = _ok(me, "/manual").text
        assert symbol in manual and f"{int(shares):,}" in manual

    # 命令行看到的现金与持仓和页面一致
    monkeypatch.setenv("QT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QT_CONFIG", str(ROOT / "config" / "default.yaml"))
    monkeypatch.setenv("DATABASE_URL", resolve_test_db_url())
    monkeypatch.setattr(qt.admin_cli, "now", lambda: clock.now)
    result = CliRunner().invoke(qt.cli.app, ["admin", "book", "show", "--email", NEWCOMER])
    assert result.exit_code == 0, result.output
    cash = _CLI_CASH.search(result.output)
    assert cash is not None, result.output
    assert cash.group(1) in manual
    holdings = dict(_CLI_HOLDING.findall(result.output))
    assert holdings == {"600003": "300", symbol: f"{int(shares):,}"}
    for code, count in holdings.items():
        assert code in manual and count in manual
