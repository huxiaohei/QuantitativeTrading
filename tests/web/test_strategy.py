"""策略页面：创建订阅、改参数（含确认页）、暂停恢复关闭、概念说明、回测报告。"""

import dataclasses
import html
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
from qt.db import repo
from qt.db.models import AuditLog, ParamChange, Subscription
from qt.service import rules
from qt.web.app import create_app
from qt.web.deps import WebDeps
from qt.web.routes import strategy
from tests.web.conftest import BASE, CFG, Clock, MakeUser, csrf_of, login_as, make_deps

# 2026-11-16（周一，今天）起的工作日
DAYS = [d for d in (date(2026, 11, 16) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]
START_DAY = DAYS[0]  # 测试时钟在周一的每日运行时刻之前：缺省起始日就是当天
NOTE = "修改参数后没有对应的回测结果可看"
_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')
STOP_LOSS = "strategy.stop_loss"
SITE_CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"


def _calendar() -> TradingCalendar:
    return TradingCalendar(DAYS)


@pytest.fixture
def deps(db: Session, db_engine: Engine, tmp_path: Path, clock: Clock) -> WebDeps:
    del db
    return make_deps(db_engine, tmp_path, clock, tmp_path / "mails", calendar=_calendar)


@pytest.fixture
def client(deps: WebDeps) -> Iterator[TestClient]:
    with TestClient(create_app(deps), base_url=BASE) as c:
        yield c


def _login(client: TestClient, make_user: MakeUser) -> int:
    user = make_user("a@example.com")
    assert login_as(client, "a@example.com").status_code == 303
    return user.id


def _post(client: TestClient, path: str, data: dict[str, str]) -> httpx.Response:
    """带会话防伪造令牌提交表单（不跟随跳转）。"""
    token = csrf_of(client.get("/strategy"))
    return client.post(path, data={**data, "csrf": token}, follow_redirects=False)


def _create(client: TestClient, cash: str = "100000", start: str = "") -> httpx.Response:
    return _post(client, "/strategy/new", {"initial_cash": cash, "start_day": start})


def _hidden(response: httpx.Response) -> dict[str, str]:
    """确认页里“确认”表单的隐藏字段（原样回传即等于点确认）。"""
    start = response.text.index('action="/strategy/params"')
    form = response.text[start : response.text.index("</form>", start)]
    return {name: html.unescape(value) for name, value in _HIDDEN.findall(form)}


def _subs(db: Session) -> list[Subscription]:
    db.expire_all()
    return list(db.scalars(select(Subscription).order_by(Subscription.id)))


def test_strategy_page_offers_creation_without_subscription(
    client: TestClient, make_user: MakeUser
) -> None:
    _login(client, make_user)
    text = client.get("/strategy").text
    assert 'href="/strategy/new"' in text
    assert "/report" not in text  # 没有配置报告文件时不显示链接


def test_create_subscription_redirects_and_shows_result(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    user_id = _login(client, make_user)
    form = client.get("/strategy/new")
    assert form.status_code == 200 and "起始资金" in form.text and str(START_DAY) in form.text
    resp = _create(client)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/strategy")
    page = client.get(resp.headers["location"])
    assert "100,000.00" in page.text and str(START_DAY) in page.text and "生效中" in page.text
    assert "订阅已创建" in page.text
    sub = repo.get_open_subscription(db, user_id)
    assert sub is not None and sub.initial_cash == 100000 and sub.start_day == START_DAY
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "subscription.create")).one()
    assert audit.channel == "web" and audit.actor_user_id == user_id


def test_create_rejects_bad_cash_and_keeps_input(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    for bad, reason in (("0", "本金必须是大于 0"), ("abc", "金额应为数字")):
        resp = _create(client, cash=bad)
        assert resp.status_code == 422, bad
        assert reason in resp.text and f'value="{bad}"' in resp.text
    resp = _create(client, start="2026-11-14")  # 周六
    assert resp.status_code == 422 and "不是交易日" in resp.text and 'value="100000"' in resp.text
    resp = _create(client, start="明天")
    assert resp.status_code == 422 and 'value="明天"' in resp.text


def test_create_with_chosen_start_day(client: TestClient, make_user: MakeUser, db: Session) -> None:
    _login(client, make_user)
    assert _create(client, start=str(DAYS[0])).status_code == 303
    assert _subs(db)[0].start_day == DAYS[0]


def test_second_open_subscription_is_rejected(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    assert _create(client).status_code == 303
    resp = _create(client)
    assert resp.status_code == 422 and "已有生效的订阅" in resp.text


def test_params_form_shows_percent_values_and_fixed_lookback(
    client: TestClient, make_user: MakeUser
) -> None:
    _login(client, make_user)
    _create(client)
    text = client.get("/strategy/params").text
    assert f'name="{STOP_LOSS}" value="{CFG.strategy.stop_loss * 100:g}"' in text
    assert 'name="strategy.lookback_days"' not in text  # 全站统一，不可改
    assert "回看天数" in text
    strategy_page = client.get("/strategy").text
    assert "止损比例" in strategy_page and f"{CFG.strategy.stop_loss * 100:g}%" in strategy_page


def test_params_confirm_then_apply(client: TestClient, make_user: MakeUser, db: Session) -> None:
    user_id = _login(client, make_user)
    _create(client)
    resp = _post(client, "/strategy/params", {STOP_LOSS: "17.5"})
    assert resp.status_code == 200
    assert str(START_DAY) in resp.text and NOTE in resp.text and "止损比例" in resp.text
    assert db.scalars(select(ParamChange)).all() == []  # 确认之前不写库

    confirmed = _post(client, "/strategy/params", _hidden(resp))
    assert confirmed.status_code == 303 and confirmed.headers["location"].startswith("/strategy")
    change = db.scalars(select(ParamChange)).one()
    assert change.effective_day == START_DAY and change.new["strategy"]["stop_loss"] == 0.175
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "subscription.update_params"))
    row = audit.one()
    assert row.channel == "web" and row.actor_user_id == user_id
    assert "17.5%" in client.get("/strategy").text


def test_params_invalid_value_is_shown_beside_field(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    _login(client, make_user)
    _create(client)
    resp = _post(client, "/strategy/params", {STOP_LOSS: "150"})
    assert resp.status_code == 422
    assert 'value="150"' in resp.text and "field-error" in resp.text
    assert db.scalars(select(ParamChange)).all() == []
    bad = _post(client, "/strategy/params", {STOP_LOSS: "很多"})
    assert bad.status_code == 422 and 'value="很多"' in bad.text and "field-error" in bad.text


def test_params_without_change_is_rejected(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    _create(client)
    same = f"{CFG.strategy.stop_loss * 100:g}"
    resp = _post(client, "/strategy/params", {STOP_LOSS: same})
    assert resp.status_code == 422 and "参数没有变化" in resp.text


def test_params_back_to_edit_keeps_values(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    _create(client)
    confirm = _post(client, "/strategy/params", {STOP_LOSS: "17.5"})
    back = _post(client, "/strategy/params", {**_hidden(confirm), "edit": "1", "confirmed": ""})
    assert back.status_code == 200 and f'name="{STOP_LOSS}" value="17.5"' in back.text


def test_params_choice_and_bool_fields(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    _login(client, make_user)
    _create(client)
    resp = _post(
        client,
        "/strategy/params",
        {"strategy.take_profit_mode": "trailing", "strategy.exclude_dividend_cut": "false"},
    )
    assert resp.status_code == 200 and "移动止盈" in resp.text
    assert _post(client, "/strategy/params", _hidden(resp)).status_code == 303
    new = db.scalars(select(ParamChange)).one().new["strategy"]
    assert new["take_profit_mode"] == "trailing" and new["exclude_dividend_cut"] is False


def test_pause_resume_close(client: TestClient, make_user: MakeUser, db: Session) -> None:
    _login(client, make_user)
    _create(client)
    assert _post(client, "/strategy/pause", {}).status_code == 303
    assert _subs(db)[0].status == "paused" and "已暂停" in client.get("/strategy").text
    assert _post(client, "/strategy/resume", {}).status_code == 303
    assert _subs(db)[0].status == "active"

    confirm = client.get("/strategy/close")
    assert confirm.status_code == 200 and "不可恢复" in confirm.text
    assert _subs(db)[0].status == "active"  # 只看确认页不关闭
    assert _post(client, "/strategy/close", {}).status_code == 303
    assert _subs(db)[0].status == "closed"
    actions = db.scalars(select(AuditLog.action).where(AuditLog.channel == "web")).all()
    assert {"subscription.pause", "subscription.resume", "subscription.close"} <= set(actions)

    page = client.get("/strategy").text
    assert "历史订阅" in page and "已关闭" in page and 'href="/strategy/new"' in page


def test_repeated_pause_shows_service_reason(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    _create(client)
    _post(client, "/strategy/pause", {})
    resp = _post(client, "/strategy/pause", {})
    assert resp.status_code == 422 and "field-error" not in resp.text
    assert "不能执行该操作" in resp.text


def test_actions_without_open_subscription_go_back_to_strategy(
    client: TestClient, make_user: MakeUser
) -> None:
    _login(client, make_user)
    for path in ("/strategy/pause", "/strategy/resume", "/strategy/close", "/strategy/params"):
        resp = _post(client, path, {})
        assert resp.status_code == 303 and resp.headers["location"] == "/strategy", path
    for path in ("/strategy/close", "/strategy/params"):
        assert client.get(path, follow_redirects=False).headers["location"] == "/strategy"


def test_pages_require_login(client: TestClient) -> None:
    for path in ("/strategy", "/strategy/new", "/strategy/params", "/concepts", "/report"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login"), path


def test_concepts_renders_markdown_without_raw_html(
    client: TestClient, make_user: MakeUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client, make_user)
    real = client.get("/concepts")
    first_heading = Path(strategy.CONCEPTS_FILE).read_text(encoding="utf-8").splitlines()[0]
    assert real.status_code == 200 and first_heading.lstrip("# ") in real.text
    source = tmp_path / "concepts.md"
    source.write_text("# 标题\n\n<script>alert(1)</script>\n\n正文 **加粗**\n", encoding="utf-8")
    monkeypatch.setattr(strategy, "CONCEPTS_FILE", source)
    text = client.get("/concepts").text
    assert "<script>alert(1)" not in text and "<strong>加粗</strong>" in text


def test_report_is_404_when_not_configured(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    assert client.get("/report").status_code == 404


def test_report_is_served_when_configured(
    deps: WebDeps, make_user: MakeUser, tmp_path: Path, db_engine: Engine
) -> None:
    report = tmp_path / "report.html"
    report.write_text("<html><body>回测报告正文</body></html>", encoding="utf-8")
    web = CFG.web.model_copy(update={"report_file": str(report)})
    configured = dataclasses.replace(deps, cfg=CFG.model_copy(update={"web": web}))
    with TestClient(create_app(configured), base_url=BASE) as c:
        _login(c, make_user)
        resp = c.get("/report")
        assert resp.status_code == 200 and "回测报告正文" in resp.text
        assert 'href="/report"' in c.get("/strategy").text
        report.unlink()
        assert c.get("/report").status_code == 404
        assert "/report" not in c.get("/strategy").text


def test_strategy_page_shows_param_descriptions(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    _create(client)
    text = client.get("/strategy").text
    stop_loss = next(p for p in rules.PARAMS if p.key == STOP_LOSS)
    assert stop_loss.description in text and "<th>说明</th>" in text


def test_params_overlong_number_is_422_not_500(client: TestClient, make_user: MakeUser) -> None:
    _login(client, make_user)
    _create(client)
    for key in (STOP_LOSS, "risk.max_positions"):
        resp = _post(client, "/strategy/params", {key: "9" * 5000})
        assert resp.status_code == 422 and "field-error" in resp.text, key


def test_report_has_own_csp_allowing_inline_styles(
    deps: WebDeps, make_user: MakeUser, tmp_path: Path
) -> None:
    """回测报告自带 <style> 和 style= 属性：只给这一页放开内联样式，并放进沙箱。"""
    report = tmp_path / "report.html"
    report.write_text(
        "<html><head><style>td { color: red; }</style></head>"
        '<body><table><tr><td style="text-align: right">1.23</td></tr></table></body></html>',
        encoding="utf-8",
    )
    web = CFG.web.model_copy(update={"report_file": str(report)})
    configured = dataclasses.replace(deps, cfg=CFG.model_copy(update={"web": web}))
    with TestClient(create_app(configured), base_url=BASE) as c:
        _login(c, make_user)
        resp = c.get("/report")
        assert resp.status_code == 200 and 'style="text-align: right"' in resp.text
        assert resp.headers.get_list("content-security-policy") == [
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
        ]
        assert c.get("/strategy").headers.get_list("content-security-policy") == [SITE_CSP]
