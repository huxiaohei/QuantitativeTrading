"""策略页面：订阅状态与参数、创建订阅、改参数（先确认）、暂停恢复关闭、概念说明、回测报告。

本模块只解析表单、调用服务、显示服务给出的中文原因；校验、生效日、"只能有一个订阅"都在服务层。
"""

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from qt.config import AppConfig
from qt.db import repo
from qt.db.models import Subscription
from qt.service import accounts, rules, views
from qt.service.actor import web_actor
from qt.service.errors import ValidationError
from qt.web.app import route
from qt.web.deps import (
    FLASH_MESSAGES,
    Current,
    CurrentUser,
    DbSession,
    Deps,
    WebDeps,
    render,
)
from qt.web.forms import FormError, parse_decimal, parse_integer, parse_money
from qt.web.security import REPORT_CSP

router = APIRouter()

# 概念说明文件：镜像里复制到同一相对位置
CONCEPTS_FILE = Path(__file__).resolve().parents[4] / "docs" / "concepts.md"
NO_BACKTEST_NOTE = (
    "注意：修改参数后没有对应的回测结果可看，已有回测报告里的数字不代表你修改后的参数。"
)
_PARAM_PREFIX = "参数 "

FormText = Annotated[str, Form()]
# 表单提交的原始文字：字段名 -> 内容
_Values = dict[str, str]


class _Preview(Exception):
    """预检用：先让服务层完整校验一遍，再回滚，什么也不留下。"""


@dataclass(frozen=True)
class ParamRow:
    """参数表的一行：说明、当前值与默认值的显示文字、表单里的输入内容与错误。"""

    info: rules.ParamInfo
    current: str
    default: str
    text: str
    error: str | None
    range_hint: str | None


# ---- 参数的取值与显示 ----


def _number(value: float) -> str:
    """去掉多余的 0：5.0 -> 5，0.175 -> 0.175。"""
    return f"{round(value, 10):.10f}".rstrip("0").rstrip(".") or "0"


def _raw(cfg: AppConfig, key: str) -> object:
    section, _, field = key.partition(".")
    return getattr(getattr(cfg, section), field)


def _as_number(value: object) -> float:
    assert isinstance(value, int | float)
    return float(value)


def _input_text(info: rules.ParamInfo, value: object) -> str:
    """表单里的文字：百分比参数按百分数显示（0.05 -> 5）。"""
    if info.kind == "percent":
        return _number(_as_number(value) * 100)
    if info.kind == "bool":
        return "true" if value else "false"
    if info.kind in ("int", "float"):
        return _number(_as_number(value))
    return str(value)


def _shown(info: rules.ParamInfo, value: object) -> str:
    """页面上给人看的值。"""
    if info.kind == "percent":
        return f"{_number(_as_number(value) * 100)}%"
    if info.kind == "bool":
        return "是" if value else "否"
    if info.kind == "choice":
        return dict(info.choices).get(str(value), str(value))
    return _input_text(info, value)


def _range_hint(info: rules.ParamInfo) -> str | None:
    if info.minimum is None and info.maximum is None:
        return None
    scale = 100 if info.kind == "percent" else 1
    unit = "%" if info.kind == "percent" else ""
    low = "不限" if info.minimum is None else f"{_number(info.minimum * scale)}{unit}"
    high = "不限" if info.maximum is None else f"{_number(info.maximum * scale)}{unit}"
    return f"取值范围 {low} 到 {high}"


def _parse_value(info: rules.ParamInfo, text: str) -> object:
    """把表单文字转成配置里的取值（百分数转回小数）；只判断格式，范围交给服务层。"""
    if info.kind == "int":
        return parse_integer(text, info.key)
    if info.kind == "float":
        return parse_decimal(text, info.key)
    if info.kind == "percent":
        return round(parse_decimal(text, info.key) / 100, 10)
    if info.kind == "bool":
        if text in ("true", "false"):
            return text == "true"
        raise FormError(info.key, "请选择“是”或“否”")
    if text not in dict(info.choices):
        raise FormError(info.key, "请选择列表里的选项")
    return text


def _same_value(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
    return left == right


def _editable() -> list[rules.ParamInfo]:
    return [info for info in rules.PARAMS if not info.site_wide]


def _collect(values: _Values, cfg: AppConfig) -> tuple[dict[str, object], dict[str, str]]:
    """按表单取出改动的参数 {路径: 新值}，以及各字段的格式错误；没提交的与没改的都跳过。"""
    overrides: dict[str, object] = {}
    errors: dict[str, str] = {}
    for info in _editable():
        text = values.get(info.key)
        if text is None:
            continue
        try:
            parsed = _parse_value(info, text.strip())
        except FormError as exc:
            errors[exc.field] = exc.message
            continue
        if not _same_value(parsed, _raw(cfg, info.key)):
            overrides[info.key] = parsed
    return overrides, errors


def _rows(deps: WebDeps, cfg: AppConfig, values: _Values, errors: dict[str, str]) -> list[ParamRow]:
    """参数表的行：values 是用户刚填的内容（没有则显示当前值）。"""
    return [
        ParamRow(
            info=info,
            current=_shown(info, _raw(cfg, info.key)),
            default=_shown(info, _raw(deps.cfg, info.key)),
            text=values.get(info.key, _input_text(info, _raw(cfg, info.key))),
            error=errors.get(info.key),
            range_hint=_range_hint(info),
        )
        for info in rules.PARAMS
    ]


def _locate(message: str) -> str | None:
    """服务层的原因里点名了哪个参数，就显示在哪个字段旁；点不出来返回 None（显示在表单顶部）。"""
    for info in _editable():
        if f"{_PARAM_PREFIX}{info.key} " in message:
            return info.key
    return None


# ---- 共用 ----


def _open_sub(s: Session, found: Current) -> Subscription | None:
    return repo.get_open_subscription(s, found.user.id)


def _to_strategy(code: str | None = None) -> Response:
    return RedirectResponse("/strategy" if code is None else f"/strategy?msg={code}", 303)


def _report_file(deps: WebDeps) -> Path | None:
    configured = deps.cfg.web.report_file
    if configured is None:
        return None
    path = Path(configured)
    return path if path.is_file() else None


def _strategy_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    s: Session,
    *,
    flash: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    sub = _open_sub(s, found)
    all_views = views.subscriptions(s, found.user.id)
    current = next((v for v in all_views if sub is not None and v.id == sub.id), None)
    rows = [] if sub is None else _rows(deps, accounts.subscription_config(deps.cfg, sub), {}, {})
    context: dict[str, object] = {
        "active": "strategy",
        "flash": flash,
        "error": error,
        "current": current,
        "rows": rows,
        "history": [v for v in all_views if v.read_only],
        "has_report": _report_file(deps) is not None,
    }
    return render(request, "strategy.html", context, found=found, status_code=status_code)


# ---- 策略页 ----


@route(router, "GET", "/strategy", "user")
def strategy_page(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, msg: str | None = None
) -> Response:
    return _strategy_page(request, found, deps, s, flash=FLASH_MESSAGES.get(msg or ""))


# ---- 创建订阅 ----


def _new_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    *,
    cash: str = "",
    start: str = "",
    errors: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    default_day: str | None = None
    try:
        default_day = str(rules.default_start_day(deps.calendar(), deps.now().date()))
    except ValidationError as exc:
        error = error or str(exc)
    context: dict[str, object] = {
        "active": "strategy",
        "cash": cash,
        "start": start,
        "errors": errors or {},
        "error": error,
        "default_day": default_day,
        "limit_days": deps.cfg.service.backdate_limit_days,
    }
    return render(request, "strategy_new.html", context, found=found, status_code=status_code)


@route(router, "GET", "/strategy/new", "user")
def new_form(request: Request, found: CurrentUser, deps: Deps) -> Response:
    return _new_page(request, found, deps)


@route(router, "POST", "/strategy/new", "user")
def new_submit(
    request: Request,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    initial_cash: FormText = "",
    start_day: FormText = "",
) -> Response:
    def failed(errors: dict[str, str] | None = None, error: str | None = None) -> Response:
        return _new_page(
            request,
            found,
            deps,
            cash=initial_cash,
            start=start_day,
            errors=errors,
            error=error,
            status_code=422,
        )

    try:
        cash = parse_money(initial_cash, "initial_cash")
    except FormError as exc:
        return failed({exc.field: exc.message})
    today = deps.now().date()
    calendar = deps.calendar()
    try:
        default = rules.default_start_day(calendar, today)
    except ValidationError as exc:
        return failed(error=str(exc))
    try:
        first = rules.resolve_start_day(
            start_day.strip() or None,
            calendar,
            default,
            deps.cfg.service.backdate_limit_days,
            today=today,
        )
    except ValidationError as exc:
        return failed({"start_day": str(exc)})
    try:
        with s.begin_nested():
            accounts.create_subscription(
                s, found.user.id, deps.cfg, cash, first, None, actor=web_actor(found.user.id)
            )
    except ValidationError as exc:
        reason = str(exc)
        return failed({"initial_cash": reason}) if "本金" in reason else failed(error=reason)
    return _to_strategy("sub_created")


# ---- 改参数 ----


def _params_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    sub: Subscription,
    *,
    values: _Values | None = None,
    errors: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    cfg = accounts.subscription_config(deps.cfg, sub)
    context: dict[str, object] = {
        "active": "strategy",
        "rows": _rows(deps, cfg, values or {}, errors or {}),
        "error": error,
    }
    return render(request, "strategy_params.html", context, found=found, status_code=status_code)


@route(router, "GET", "/strategy/params", "user")
def params_form(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    return _params_page(request, found, deps, sub)


def _apply(
    s: Session,
    found: Current,
    sub: Subscription,
    deps: WebDeps,
    overrides: dict[str, object],
    effective: date,
    *,
    save: bool,
) -> None:
    """交给服务层修改参数；save 为假时只预检：服务层认可后回滚，什么也不留下。"""
    try:
        with s.begin_nested():
            accounts.update_params(
                s,
                found.user.id,
                sub.id,
                deps.cfg,
                overrides,
                effective,
                actor=web_actor(found.user.id),
            )
            if not save:
                raise _Preview
    except _Preview:
        pass


def _params_submit(
    request: Request, found: Current, deps: WebDeps, s: Session, values: _Values
) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    if "edit" in values:  # 从确认页返回修改：带着已填的内容回到表单
        return _params_page(request, found, deps, sub, values=values)
    cfg = accounts.subscription_config(deps.cfg, sub)
    overrides, errors = _collect(values, cfg)
    if errors:
        return _params_page(
            request, found, deps, sub, values=values, errors=errors, status_code=422
        )
    try:
        effective = rules.next_effective_day(s, sub, deps.calendar())
        confirmed = "confirmed" in values
        _apply(s, found, sub, deps, overrides, effective, save=confirmed)
    except ValidationError as exc:
        reason = str(exc)
        field = _locate(reason)
        return _params_page(
            request,
            found,
            deps,
            sub,
            values=values,
            errors=None if field is None else {field: reason},
            error=reason if field is None else None,
            status_code=422,
        )
    if confirmed:
        return _to_strategy("params_saved")
    infos = {info.key: info for info in rules.PARAMS}
    changes = [
        (infos[key].label, _shown(infos[key], _raw(cfg, key)), _shown(infos[key], value))
        for key, value in overrides.items()
    ]
    carried = [(key, values[key]) for key in overrides] + [("confirmed", "1")]
    context: dict[str, object] = {
        "active": "strategy",
        "title": "确认修改参数",
        "intro": f"从 {effective} 起，每日运行改用下面的新参数；已经处理过的日子、账本与历史不变。",
        "changes": changes,
        "notes": [NO_BACKTEST_NOTE],
        "action": "/strategy/params",
        "fields": carried,
        "confirm_label": "确认修改",
        "back_fields": [(key, values[key]) for key in overrides] + [("edit", "1")],
        "back_url": "/strategy/params",
    }
    return render(request, "confirm.html", context, found=found)


@route(router, "POST", "/strategy/params", "user")
async def params_submit(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    form = await request.form()
    values = {k: v for k, v in form.items() if isinstance(v, str)}
    return await run_in_threadpool(_params_submit, request, found, deps, s, values)


# ---- 暂停、恢复、关闭 ----


def _transition(request: Request, found: Current, deps: WebDeps, s: Session, kind: str) -> Response:
    sub = _open_sub(s, found)
    if sub is None:
        return _to_strategy()
    actor = web_actor(found.user.id)
    try:
        with s.begin_nested():
            if kind == "pause":
                accounts.pause_subscription(s, found.user.id, sub.id, actor=actor)
            elif kind == "resume":
                accounts.resume_subscription(s, found.user.id, sub.id, actor=actor)
            else:
                accounts.close_subscription(s, found.user.id, sub.id, actor=actor)
    except ValidationError as exc:
        return _strategy_page(request, found, deps, s, error=str(exc), status_code=422)
    return _to_strategy(
        {"pause": "sub_paused", "resume": "sub_resumed", "close": "sub_closed"}[kind]
    )


@route(router, "POST", "/strategy/pause", "user")
def pause(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    return _transition(request, found, deps, s, "pause")


@route(router, "POST", "/strategy/resume", "user")
def resume(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    return _transition(request, found, deps, s, "resume")


@route(router, "GET", "/strategy/close", "user")
def close_confirm(request: Request, found: CurrentUser, s: DbSession) -> Response:
    if _open_sub(s, found) is None:
        return _to_strategy()
    context: dict[str, object] = {
        "active": "strategy",
        "title": "确认关闭订阅",
        "intro": "关闭后不再出新信号，且不可恢复；账本与历史会保留，可在历史订阅里只读查看。"
        "之后想继续，需要重新创建订阅。",
        "changes": [],
        "notes": [],
        "action": "/strategy/close",
        "fields": [],
        "confirm_label": "确认关闭",
        "back_fields": None,
        "back_url": "/strategy",
    }
    return render(request, "confirm.html", context, found=found)


@route(router, "POST", "/strategy/close", "user")
def close_submit(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    return _transition(request, found, deps, s, "close")


# ---- 概念说明与回测报告 ----


def _markdown(text: str) -> str:
    """把 Markdown 转成 HTML；关闭原始 HTML，文件里的标签只会按文字显示。"""
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


@route(router, "GET", "/concepts", "user")
def concepts(request: Request, found: CurrentUser) -> Response:
    body = _markdown(CONCEPTS_FILE.read_text(encoding="utf-8"))
    return render(request, "concepts.html", {"active": "", "body": body}, found=found)


@route(router, "GET", "/report", "user")
def report(deps: Deps, found: CurrentUser) -> Response:
    del found
    path = _report_file(deps)
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, headers={"Content-Security-Policy": REPORT_CSP})
