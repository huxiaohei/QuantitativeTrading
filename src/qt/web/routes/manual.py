"""手动账页面：查看持仓与流水，录入买入卖出、已有持仓、入金出金，作废一条流水。

本模块只解析表单、调用服务、显示服务给出的中文原因；板块限制、日期范围、持仓与现金是否足够、
费用估算、信号关联都在服务层（qt.service.manual_ops）。写操作只对当前未关闭的订阅开放，
路由不接受订阅编号；网址里的流水、信号编号都按当前使用者名下查找，别人的编号一律 404。
"""

from collections.abc import Callable
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from qt.db import repo
from qt.db.models import LedgerEntry, Subscription
from qt.models import Side
from qt.service import form_keys, manual_ops, views
from qt.service.actor import web_actor
from qt.service.errors import LedgerInconsistentError, NotFoundError, ValidationError
from qt.service.manual_book import check_symbol
from qt.service.market import StoreMarket, known_symbols
from qt.web.app import route
from qt.web.deps import FLASH_MESSAGES, Current, CurrentUser, DbSession, Deps, WebDeps, render
from qt.web.forms import FormError, parse_date, parse_money, parse_shares
from qt.web.routes.common import stock_names

router = APIRouter()

FormText = Annotated[str, Form()]
_Values = dict[str, str]
_ONLY_MAIN_BOARD = "目前只支持沪深主板股票"
_FAILED = (ValidationError, LedgerInconsistentError)
_STALE_FORM = "表单已失效，请重新填写后提交"
_REPAIR_HINT = "可以到手动账页的流水里作废有问题的记录，或补录入金"
_SIDES = {"buy": Side.BUY, "sell": Side.SELL}
_CASH_KINDS: dict[str, Literal["deposit", "withdraw"]] = {
    "deposit": "deposit",
    "withdraw": "withdraw",
}


class _Duplicate(Exception):
    """这个表单（同一个一次性值）已经提交成功过。"""


def _reason(exc: ValidationError | LedgerInconsistentError) -> str:
    """显示给使用者的原因：账本不一致时不带命令行的修复办法，改成网页上能做的。"""
    if isinstance(exc, LedgerInconsistentError):
        return f"{exc.reason}。{_REPAIR_HINT}"
    return str(exc)


# ---- 共用 ----


def _open_sub(s: Session, found: Current) -> Subscription | None:
    return repo.get_open_subscription(s, found.user.id)


def _to_strategy() -> Response:
    return RedirectResponse("/strategy", 303)


def _done(code: str, entry: LedgerEntry) -> Response:
    return RedirectResponse(f"/manual?msg={code}&entry={entry.id}", 303)


def _latest_day(deps: WebDeps) -> str:
    """表单里日期的默认值：不晚于今天的最近一个交易日（只用于预填，可改）。"""
    calendar = deps.calendar()
    days = calendar.between(calendar.first_day, deps.now().date())
    return str(days[-1]) if days else ""


def _market(
    deps: WebDeps, s: Session, found: Current, sub: Subscription, extra: str | None = None
) -> StoreMarket:
    """只覆盖这本账涉及的股票；使用者新填的、账里还没有的代码也要加进去。"""
    symbols = set(views.book_symbols(s, found.user.id, sub.id))
    if extra:
        symbols.add(extra)
    return deps.market(deps.now().date(), symbols)


def _field[T](
    errors: dict[str, str], parse: Callable[[str, str], T], text: str, name: str
) -> T | None:
    """解析一个字段；格式不对记下原因并返回 None。"""
    try:
        return parse(text, name)
    except FormError as exc:
        errors[exc.field] = exc.message
        return None


def _code(errors: dict[str, str], text: str) -> str:
    code = text.strip()
    if not code:
        errors["symbol"] = "请填写股票代码，例如 600036"
    return code


def _write(
    deps: WebDeps,
    s: Session,
    found: Current,
    sub: Subscription,
    extra: str | None,
    action: Callable[[StoreMarket, date], LedgerEntry],
    form_key: str | None = None,
) -> LedgerEntry:
    """在保存点里调用服务层：失败时回滚这一次写入（含对表单值的认领），不留半截数据。

    form_key 不为空时先认领表单的一次性值，已经认领过抛 _Duplicate，什么也不写。
    """
    rows = _market(deps, s, found, sub, extra)
    with s.begin_nested():
        if form_key is not None and not form_keys.claim_form(
            s, found.user.id, form_key, deps.now()
        ):
            raise _Duplicate
        return action(rows, deps.now().date())


def _unknown_symbol(deps: WebDeps, code: str) -> str | None:
    """代码不是可录入的股票时服务给出的原因；只读证券表，不建行情（乱填的代码不能拖慢别人）。"""
    try:
        check_symbol(known_symbols(deps.store, deps.cfg), code)
    except ValidationError as exc:
        return str(exc)
    return None


def _duplicate() -> Response:
    return RedirectResponse("/manual?msg=manual_duplicate", 303)


def _yuan(value: float) -> str:
    return f"{value:,.2f}"


def _entry_text(e: views.EntryRow) -> str:
    """一条流水的一行摘要（成功提示与作废确认页用）。"""
    stock = f"{e.symbol} {e.name}".strip() if e.symbol else ""
    if e.kind in ("deposit", "withdraw"):
        body = f"{e.kind_label} {_yuan(e.amount)} 元，日期 {e.day}"
    elif e.kind == "void":
        body = f"作废流水 #{e.voids_entry_id}"
    elif e.kind == "transfer_in":
        body = (
            f"{e.kind_label} {stock} {e.shares:,.0f} 股，成本价 {_yuan(e.price)}，"
            f"买入日 {e.day}，从 {e.effective_day} 起进入净值"
        )
    else:
        estimated = "（估算）" if e.fees_estimated else ""
        body = (
            f"{e.kind_label} {stock} {e.shares:,.0f} 股 @ {_yuan(e.price)}，日期 {e.day}，"
            f"费用 {_yuan(e.fees)}{estimated}"
        )
        if e.signal_id is not None:
            body += f"，关联信号 #{e.signal_id}"
    if e.note:
        body += f"；提示：{e.note}"
    return f"{body}（流水 #{e.id}）"


# ---- 手动账页 ----


def _manual_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    s: Session,
    sub: Subscription,
    *,
    msg: str | None = None,
    entry: int | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    today = deps.now().date()
    page = views.manual_page(
        s,
        found.user.id,
        sub.id,
        _market(deps, s, found, sub),
        deps.calendar(),
        deps.cfg,
        today,
        names=stock_names(deps),
    )
    flash = FLASH_MESSAGES.get(msg or "")
    if entry is not None:
        # 网址里的流水编号只在本人当前手动账里找：别人的、不存在的一律 404，不当作没给
        row = next((e for e in page.entries if e.id == entry), None)
        if row is None:
            raise NotFoundError("流水不存在")
        if flash is not None:
            flash = f"{flash}：{_entry_text(row)}"
    context: dict[str, object] = {
        "active": "manual",
        "flash": flash,
        "error": error,
        "page": page,
    }
    return render(request, "manual.html", context, found=found, status_code=status_code)


@route(router, "GET", "/manual", "user")
def manual_view(
    request: Request,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    msg: str | None = None,
    entry: int | None = None,
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    return _manual_page(request, found, deps, s, sub, msg=msg, entry=entry)


# ---- 买入、卖出 ----


def _trade_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    *,
    values: _Values,
    errors: dict[str, str] | None = None,
    error: str | None = None,
    signal: views.SignalRow | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "manual",
        "values": values,
        "errors": errors or {},
        "error": error,
        "signal": signal,
        "form_key": form_keys.new_form_value(),
        "board_note": _ONLY_MAIN_BOARD,
    }
    return render(request, "manual_trade.html", context, found=found, status_code=status_code)


@route(router, "GET", "/manual/trade", "user")
def trade_form(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, signal: int | None = None
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    values: _Values = {"side": "buy", "day": _latest_day(deps)}
    row: views.SignalRow | None = None
    if signal is not None:
        row = views.recordable_signal(
            s,
            found.user.id,
            sub.id,
            signal,
            deps.calendar(),
            deps.cfg,
            deps.now().date(),
            names=stock_names(deps),
        )
        values.update(side=row.side, symbol=row.symbol, shares=f"{row.shares:.0f}")
    return _trade_page(request, found, deps, values=values, signal=row)


@route(router, "POST", "/manual/trade", "user")
def trade_submit(
    request: Request,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    side: FormText = "",
    symbol: FormText = "",
    day: FormText = "",
    price: FormText = "",
    shares: FormText = "",
    fees: FormText = "",
    form_key: FormText = "",
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    values = {
        "side": side,
        "symbol": symbol,
        "day": day,
        "price": price,
        "shares": shares,
        "fees": fees,
    }
    if not form_keys.is_valid_form_value(form_key):
        return _trade_page(request, found, deps, values=values, error=_STALE_FORM, status_code=422)
    errors: dict[str, str] = {}
    code = _code(errors, symbol)
    if code and (reason := _unknown_symbol(deps, code)) is not None:
        errors["symbol"] = reason
    trade_side = _SIDES.get(side)
    if trade_side is None:
        errors["side"] = "请选择买入或卖出"
    trade_day = _field(errors, parse_date, day, "day")
    trade_price = _field(errors, parse_money, price, "price")
    trade_shares = _field(errors, parse_shares, shares, "shares")
    trade_fees = None if not fees.strip() else _field(errors, parse_money, fees, "fees")
    if (
        errors
        or trade_side is None
        or trade_day is None
        or trade_price is None
        or trade_shares is None
    ):
        return _trade_page(request, found, deps, values=values, errors=errors, status_code=422)
    user_id = found.user.id
    calendar = deps.calendar()

    def action(market: StoreMarket, today: date) -> LedgerEntry:
        return manual_ops.record_trade(
            s,
            user_id,
            sub.id,
            market,
            calendar,
            deps.cfg,
            today,
            trade_side,
            code,
            trade_day,
            trade_price,
            trade_shares,
            trade_fees,
            actor=web_actor(user_id),
        )

    try:
        entry = _write(deps, s, found, sub, code, action, form_key)
    except _Duplicate:
        return _duplicate()
    except _FAILED as exc:
        return _trade_page(request, found, deps, values=values, error=_reason(exc), status_code=422)
    return _done("manual_trade", entry)


# ---- 已有持仓 ----


def _holding_page(
    request: Request,
    found: Current,
    *,
    values: _Values,
    errors: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "manual",
        "values": values,
        "errors": errors or {},
        "error": error,
        "form_key": form_keys.new_form_value(),
        "board_note": _ONLY_MAIN_BOARD,
    }
    return render(request, "manual_holding.html", context, found=found, status_code=status_code)


@route(router, "GET", "/manual/holding", "user")
def holding_form(request: Request, found: CurrentUser, s: DbSession) -> Response:
    if _open_sub(s, found) is None:
        return _to_strategy()
    return _holding_page(request, found, values={})


@route(router, "POST", "/manual/holding", "user")
def holding_submit(
    request: Request,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    symbol: FormText = "",
    buy_day: FormText = "",
    cost_price: FormText = "",
    shares: FormText = "",
    form_key: FormText = "",
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    values = {"symbol": symbol, "buy_day": buy_day, "cost_price": cost_price, "shares": shares}
    if not form_keys.is_valid_form_value(form_key):
        return _holding_page(request, found, values=values, error=_STALE_FORM, status_code=422)
    errors: dict[str, str] = {}
    code = _code(errors, symbol)
    if code and (reason := _unknown_symbol(deps, code)) is not None:
        errors["symbol"] = reason
    day = _field(errors, parse_date, buy_day, "buy_day")
    cost = _field(errors, parse_money, cost_price, "cost_price")
    count = _field(errors, parse_shares, shares, "shares")
    if errors or day is None or cost is None or count is None:
        return _holding_page(request, found, values=values, errors=errors, status_code=422)
    user_id = found.user.id
    calendar = deps.calendar()

    def action(market: StoreMarket, today: date) -> LedgerEntry:
        return manual_ops.record_holding(
            s,
            user_id,
            sub.id,
            market,
            calendar,
            deps.cfg,
            today,
            code,
            day,
            cost,
            count,
            actor=web_actor(user_id),
        )

    try:
        entry = _write(deps, s, found, sub, code, action, form_key)
    except _Duplicate:
        return _duplicate()
    except _FAILED as exc:
        return _holding_page(request, found, values=values, error=_reason(exc), status_code=422)
    return _done("manual_holding", entry)


# ---- 入金、出金 ----


def _cash_page(
    request: Request,
    found: Current,
    *,
    values: _Values,
    errors: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "manual",
        "values": values,
        "errors": errors or {},
        "error": error,
        "form_key": form_keys.new_form_value(),
    }
    return render(request, "manual_cash.html", context, found=found, status_code=status_code)


@route(router, "GET", "/manual/cash", "user")
def cash_form(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    if _open_sub(s, found) is None:
        return _to_strategy()
    return _cash_page(request, found, values={"kind": "deposit", "day": _latest_day(deps)})


@route(router, "POST", "/manual/cash", "user")
def cash_submit(
    request: Request,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    kind: FormText = "",
    day: FormText = "",
    amount: FormText = "",
    form_key: FormText = "",
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    values = {"kind": kind, "day": day, "amount": amount}
    if not form_keys.is_valid_form_value(form_key):
        return _cash_page(request, found, values=values, error=_STALE_FORM, status_code=422)
    errors: dict[str, str] = {}
    cash_kind = _CASH_KINDS.get(kind)
    if cash_kind is None:
        errors["kind"] = "请选择入金或出金"
    cash_day = _field(errors, parse_date, day, "day")
    money = _field(errors, parse_money, amount, "amount")
    if errors or cash_kind is None or cash_day is None or money is None:
        return _cash_page(request, found, values=values, errors=errors, status_code=422)
    user_id = found.user.id
    calendar = deps.calendar()

    def action(market: StoreMarket, today: date) -> LedgerEntry:
        return manual_ops.record_cash(
            s,
            user_id,
            sub.id,
            market,
            calendar,
            deps.cfg,
            today,
            cash_kind,
            cash_day,
            money,
            actor=web_actor(user_id),
        )

    try:
        entry = _write(deps, s, found, sub, None, action, form_key)
    except _Duplicate:
        return _duplicate()
    except _FAILED as exc:
        return _cash_page(request, found, values=values, error=_reason(exc), status_code=422)
    return _done(f"manual_{cash_kind}", entry)


# ---- 作废 ----


def _own_entry(
    deps: WebDeps, s: Session, found: Current, sub: Subscription, entry_id: int
) -> views.EntryRow:
    """当前订阅手动账里的一条流水；别人的或不存在的抛 NotFoundError。"""
    page = views.manual_page(
        s,
        found.user.id,
        sub.id,
        _market(deps, s, found, sub),
        deps.calendar(),
        deps.cfg,
        deps.now().date(),
        names=stock_names(deps),
    )
    for row in page.entries:
        if row.id == entry_id:
            return row
    raise NotFoundError("流水不存在")


@route(router, "GET", "/manual/entries/{entry_id}/void", "user")
def void_confirm(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, entry_id: int
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    row = _own_entry(deps, s, found, sub, entry_id)
    if not row.can_void:
        return RedirectResponse("/manual?msg=void_na", 303)
    context: dict[str, object] = {
        "active": "manual",
        "title": "确认作废流水",
        "intro": f"要作废的流水：{_entry_text(row)}",
        "changes": [],
        "notes": [
            "作废会追加一条作废记录，原记录保留；现金与持仓按剩下的流水重新计算。"
            "已经关联的信号状态不会改回。"
        ],
        "action": f"/manual/entries/{entry_id}/void",
        "fields": [],
        "confirm_label": "确认作废",
        "back_fields": None,
        "back_url": "/manual",
    }
    return render(request, "confirm.html", context, found=found)


@route(router, "POST", "/manual/entries/{entry_id}/void", "user")
def void_submit(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, entry_id: int
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    user_id = found.user.id
    calendar = deps.calendar()

    def action(market: StoreMarket, today: date) -> LedgerEntry:
        return manual_ops.void_entry(
            s,
            user_id,
            sub.id,
            market,
            calendar,
            deps.cfg,
            today,
            entry_id,
            actor=web_actor(user_id),
        )

    try:
        entry = _write(deps, s, found, sub, None, action)
    except _FAILED as exc:
        return _manual_page(request, found, deps, s, sub, error=_reason(exc), status_code=422)
    return _done("manual_void", entry)
