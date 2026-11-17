"""管理员页面：邀请码（生成、作废、改备注、删除）、使用者启停与备注、运行记录、立即重试、失败邮件重新排队。

路由全部是 access="admin"，非管理员一律 404。本模块只解析表单、调用服务、显示服务给出的中文原因。

邀请码、使用者列表、运行记录这些页面只有账号层面的信息。管理员另可以只读查看某个使用者的手动账、
系统账和策略参数（/admin/users/{user_id}/manual、system、strategy；2026-11-17 起，
使用条款第八节已写明）：
这些路由只有 GET，不提供任何替使用者录入、作废、改参数的入口；每次打开页面记一条审计日志，
持仓表的定时刷新 10 分钟内只记一条。
使用者的信号、日志、总览不在其中。
"""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from qt.db import admin_repo, repo
from qt.db.models import User
from qt.mail import outbox
from qt.service import accounts, auth, reruns, views
from qt.service.actor import web_actor
from qt.service.errors import AuthError
from qt.service.views import admin as admin_views
from qt.web.app import route
from qt.web.deps import FLASH_MESSAGES, Current, CurrentUser, DbSession, Deps, WebDeps, render
from qt.web.routes.common import live_context, stock_names
from qt.web.routes.strategy import param_rows

router = APIRouter()

FormText = Annotated[str, Form()]
NOTE_MAX_LENGTH = auth.NOTE_MAX_LENGTH
RUNS_SHOWN = 30
NOTHING_TO_RETRY = "没有未完成的交易日"


def _redirect(path: str, code: str) -> Response:
    return RedirectResponse(f"{path}?msg={code}", status_code=303)


# ---- 邀请码 ----


def _invites_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    s: Session,
    *,
    flash: str | None = None,
    error: str | None = None,
    note: str = "",
    status_code: int = 200,
) -> Response:
    base = deps.base_url.rstrip("/")
    rows = [
        (row, None if row.code is None else f"{base}/register?invite={quote(row.code, safe='')}")
        for row in admin_views.invite_rows(s, deps.now())
    ]
    context: dict[str, object] = {
        "active": "admin",
        "section": "invites",
        "rows": rows,
        "flash": flash,
        "error": error,
        "note": note,
        "valid_days": deps.cfg.web.invite_valid_days,
        "note_max": NOTE_MAX_LENGTH,
    }
    return render(request, "admin_invites.html", context, found=found, status_code=status_code)


@route(router, "GET", "/admin/invites", "admin")
def invites_page(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, msg: str | None = None
) -> Response:
    return _invites_page(request, found, deps, s, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "POST", "/admin/invites", "admin")
def invites_create(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, note: FormText = ""
) -> Response:
    try:
        cleaned = auth.clean_note(note)
    except AuthError as exc:
        return _invites_page(request, found, deps, s, error=str(exc), note=note, status_code=422)
    auth.create_invite(s, found.user.id, cleaned, deps.cfg.web.invite_valid_days, deps.now())
    return _redirect("/admin/invites", "invite_created")


@route(router, "POST", "/admin/invites/{invite_id}/revoke", "admin")
def invites_revoke(
    request: Request, invite_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    try:
        auth.revoke_invite(s, found.user.id, invite_id, deps.now())
    except AuthError as exc:
        return _invites_page(request, found, deps, s, error=str(exc), status_code=422)
    return _redirect("/admin/invites", "invite_revoked")


def _note_page(
    request: Request,
    found: Current,
    *,
    section: str,
    subject: str,
    action: str,
    back_url: str,
    note: str,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """改备注的表单页（邀请码与使用者共用）。subject 是在改谁的备注。"""
    context: dict[str, object] = {
        "active": "admin",
        "section": section,
        "subject": subject,
        "action": action,
        "back_url": back_url,
        "note": note,
        "note_max": NOTE_MAX_LENGTH,
        "error": error,
    }
    return render(request, "admin_note.html", context, found=found, status_code=status_code)


def _invite_subject(row: admin_views.InviteRow) -> str:
    created = row.created_at.strftime("%Y-%m-%d %H:%M")
    return f"{created} 创建的邀请码（{row.state_label}）"


def _invite_row(s: Session, deps: WebDeps, invite_id: int) -> admin_views.InviteRow:
    return admin_views.invite_row(admin_repo.get_invite(s, invite_id), deps.now())


def _invite_note_page(
    request: Request,
    found: Current,
    row: admin_views.InviteRow,
    note: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return _note_page(
        request,
        found,
        section="invites",
        subject=_invite_subject(row),
        action=f"/admin/invites/{row.id}/note",
        back_url="/admin/invites",
        note=note,
        error=error,
        status_code=status_code,
    )


@route(router, "GET", "/admin/invites/{invite_id}/note", "admin")
def invites_note_form(
    request: Request, invite_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    row = _invite_row(s, deps, invite_id)
    return _invite_note_page(request, found, row, row.note)


@route(router, "POST", "/admin/invites/{invite_id}/note", "admin")
def invites_note_save(
    request: Request,
    invite_id: int,
    found: CurrentUser,
    deps: Deps,
    s: DbSession,
    note: FormText = "",
) -> Response:
    row = _invite_row(s, deps, invite_id)
    try:
        auth.set_invite_note(s, found.user.id, invite_id, note)
    except AuthError as exc:
        return _invite_note_page(request, found, row, note, error=str(exc), status_code=422)
    return _redirect("/admin/invites", "invite_note_saved")


@route(router, "GET", "/admin/invites/{invite_id}/delete", "admin")
def invites_delete_confirm(
    request: Request, invite_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    """删除前先确认。"""
    row = _invite_row(s, deps, invite_id)
    noted = f"，备注“{row.note}”" if row.note else ""
    notes = {
        "usable": ["这条邀请码还没有被使用，删除后它的注册链接立即失效。"],
        "used": ["这条邀请码已被使用，删除不影响用它注册的账号。"],
    }
    context: dict[str, object] = {
        "active": "admin",
        "title": "确认删除邀请码",
        "intro": f"删除{_invite_subject(row)}{noted}。删除后这一行从列表里消失，不能恢复；"
        "操作日志里仍留有记录。",
        "changes": [],
        "notes": notes.get(row.state, []),
        "action": f"/admin/invites/{row.id}/delete",
        "fields": [],
        "confirm_label": "确认删除",
        "back_fields": None,
        "back_url": "/admin/invites",
    }
    return render(request, "confirm.html", context, found=found)


@route(router, "POST", "/admin/invites/{invite_id}/delete", "admin")
def invites_delete(
    request: Request, invite_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    del request
    auth.delete_invite(s, found.user.id, invite_id, deps.now())
    return _redirect("/admin/invites", "invite_deleted")


# ---- 使用者 ----


def _users_page(
    request: Request,
    found: Current,
    s: Session,
    *,
    flash: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "admin",
        "section": "users",
        "rows": admin_views.user_rows(s),
        "me": found.user.id,
        "flash": flash,
        "error": error,
    }
    return render(request, "admin_users.html", context, found=found, status_code=status_code)


@route(router, "GET", "/admin/users", "admin")
def users_page(
    request: Request, found: CurrentUser, s: DbSession, msg: str | None = None
) -> Response:
    return _users_page(request, found, s, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "GET", "/admin/users/{user_id}/disable", "admin")
def users_disable_confirm(
    request: Request, user_id: int, found: CurrentUser, s: DbSession
) -> Response:
    """停用前先确认。"""
    target = repo.get_user(s, user_id)
    context: dict[str, object] = {
        "active": "admin",
        "title": "确认停用账号",
        "intro": f"停用 {target.email} 后，该账号的全部登录立即失效，也不能再登录；"
        "数据保留，可以随时恢复。",
        "changes": [],
        "notes": [],
        "action": f"/admin/users/{target.id}/disable",
        "fields": [],
        "confirm_label": "确认停用",
        "back_fields": None,
        "back_url": "/admin/users",
    }
    return render(request, "confirm.html", context, found=found)


def _set_status(
    request: Request,
    user_id: int,
    status: Literal["active", "disabled"],
    found: Current,
    deps: WebDeps,
    s: Session,
) -> Response:
    try:
        auth.set_user_status(s, found.user.id, user_id, status, deps.now())
    except AuthError as exc:
        return _users_page(request, found, s, error=str(exc), status_code=422)
    return _redirect("/admin/users", "user_disabled" if status == "disabled" else "user_enabled")


@route(router, "POST", "/admin/users/{user_id}/disable", "admin")
def users_disable(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    return _set_status(request, user_id, "disabled", found, deps, s)


@route(router, "POST", "/admin/users/{user_id}/enable", "admin")
def users_enable(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    return _set_status(request, user_id, "active", found, deps, s)


def _user_note_page(
    request: Request,
    found: Current,
    user_id: int,
    email: str,
    note: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return _note_page(
        request,
        found,
        section="users",
        subject=email,
        action=f"/admin/users/{user_id}/note",
        back_url="/admin/users",
        note=note,
        error=error,
        status_code=status_code,
    )


@route(router, "GET", "/admin/users/{user_id}/note", "admin")
def users_note_form(request: Request, user_id: int, found: CurrentUser, s: DbSession) -> Response:
    target = repo.get_user(s, user_id)
    return _user_note_page(request, found, target.id, target.email, target.note)


@route(router, "POST", "/admin/users/{user_id}/note", "admin")
def users_note_save(
    request: Request, user_id: int, found: CurrentUser, s: DbSession, note: FormText = ""
) -> Response:
    target = repo.get_user(s, user_id)
    try:
        auth.set_user_note(s, found.user.id, user_id, note)
    except AuthError as exc:
        return _user_note_page(
            request, found, target.id, target.email, note, error=str(exc), status_code=422
        )
    return _redirect("/admin/users", "user_note_saved")


# ---- 只读查看某个使用者的手动账、系统账、策略 ----

_VIEW_TABS = {"manual": "手动账", "system": "系统账", "strategy": "策略"}


def _view_base(target: User, tab: str) -> dict[str, object]:
    """查看页共用的模板数据。target 只来自网址里的使用者编号，且本模块的路由都要求管理员。"""
    return {
        "active": "admin",
        "section": "users",
        "target": target,
        "tab": tab,
        "tabs": _VIEW_TABS,
    }


_VIEW_LOG_MINUTES = 10


def _record_view(s: Session, found: Current, target: User, tab: str, *, always: bool) -> None:
    """记一条"管理员查看了谁的哪一页"。打开整页每次都记（always）；持仓表这一块在交易时段会被
    定时请求，同一管理员对同一人同一本账 _VIEW_LOG_MINUTES 分钟内只记一条——直接请求这一块
    （不经过整页）也因此留痕。
    """
    key = f"user:{target.id}"
    if not always:
        recent = admin_repo.recent_audit_details(
            s, found.user.id, "admin_view", key, _VIEW_LOG_MINUTES
        )
        if any(detail.get("page") == tab for detail in recent):
            return
    repo.add_audit(s, target.id, "admin_view", key, {"page": tab}, web_actor(found.user.id))


def _manual_of(deps: WebDeps, s: Session, target: User) -> views.ManualBookPage | None:
    sub = views.resolve_subscription(s, target.id, None)
    if sub is None:
        return None
    market = deps.market(deps.now().date(), views.book_symbols(s, target.id, sub.id))
    return views.manual_page(
        s,
        target.id,
        sub.id,
        market,
        deps.calendar(),
        deps.cfg,
        deps.now().date(),
        names=stock_names(deps),
    )


def _system_of(deps: WebDeps, s: Session, target: User) -> views.SystemBookView | None:
    sub = views.resolve_subscription(s, target.id, None)
    if sub is None:
        return None
    market = deps.market(deps.now().date(), views.book_symbols(s, target.id, sub.id))
    return views.system_book(s, target.id, sub.id, market, names=stock_names(deps))


@route(router, "GET", "/admin/users/{user_id}/manual", "admin")
def view_manual(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    target = repo.get_user(s, user_id)
    page = _manual_of(deps, s, target)
    rows = [] if page is None else page.holdings
    url = f"/admin/users/{target.id}/manual/holdings"
    context = {
        **_view_base(target, "manual"),
        "page": page,
        **live_context(deps, rows, url, levels=True),
    }
    _record_view(s, found, target, "manual", always=True)
    return render(request, "admin_user_view.html", context, found=found)


@route(router, "GET", "/admin/users/{user_id}/manual/holdings", "admin")
def view_manual_holdings(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    """查看页里手动账持仓表这一块，供交易时段定时替换（审计日志 10 分钟内不重复记）。"""
    target = repo.get_user(s, user_id)
    page = _manual_of(deps, s, target)
    rows = [] if page is None else page.holdings
    url = f"/admin/users/{target.id}/manual/holdings"
    context = live_context(deps, rows, url, levels=True)
    _record_view(s, found, target, "manual", always=False)
    return render(request, "_holdings_live.html", context, found=found)


@route(router, "GET", "/admin/users/{user_id}/system", "admin")
def view_system(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    target = repo.get_user(s, user_id)
    view = _system_of(deps, s, target)
    rows = [] if view is None else view.holdings
    url = f"/admin/users/{target.id}/system/holdings"
    context = {
        **_view_base(target, "system"),
        "view": view,
        **live_context(deps, rows, url, levels=False),
    }
    _record_view(s, found, target, "system", always=True)
    return render(request, "admin_user_view.html", context, found=found)


@route(router, "GET", "/admin/users/{user_id}/system/holdings", "admin")
def view_system_holdings(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    """查看页里系统账持仓表这一块，供交易时段定时替换（审计日志 10 分钟内不重复记）。"""
    target = repo.get_user(s, user_id)
    view = _system_of(deps, s, target)
    rows = [] if view is None else view.holdings
    url = f"/admin/users/{target.id}/system/holdings"
    context = live_context(deps, rows, url, levels=False)
    _record_view(s, found, target, "system", always=False)
    return render(request, "_holdings_live.html", context, found=found)


@route(router, "GET", "/admin/users/{user_id}/strategy", "admin")
def view_strategy(
    request: Request, user_id: int, found: CurrentUser, deps: Deps, s: DbSession
) -> Response:
    target = repo.get_user(s, user_id)
    sub = views.resolve_subscription(s, target.id, None)
    all_views = views.subscriptions(s, target.id)
    current = next((v for v in all_views if sub is not None and v.id == sub.id), None)
    rows = (
        [] if sub is None else param_rows(deps, accounts.subscription_config(deps.cfg, sub), {}, {})
    )
    context = {
        **_view_base(target, "strategy"),
        "current": current,
        "rows": rows,
        "history": [v for v in all_views if current is None or v.id != current.id],
    }
    _record_view(s, found, target, "strategy", always=True)
    return render(request, "admin_user_view.html", context, found=found)


# ---- 运行记录与重试 ----


def _can_retry(deps: WebDeps, s: Session) -> bool:
    return reruns.has_unfinished_day(s, deps.calendar(), deps.now(), deps.cfg.service.run_time)


def _runs_page(
    request: Request,
    found: Current,
    deps: WebDeps,
    s: Session,
    *,
    flash: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "admin",
        "section": "runs",
        "view": views.recent_runs(s, RUNS_SHOWN, hide_error_messages=True),
        "rerun": reruns.status_text(reruns.latest(s)),
        "rerun_pending": reruns.has_pending(s),
        "can_retry": _can_retry(deps, s),
        "flash": flash,
        "error": error,
    }
    return render(request, "admin_runs.html", context, found=found, status_code=status_code)


@route(router, "GET", "/admin/runs", "admin")
def runs_page(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, msg: str | None = None
) -> Response:
    return _runs_page(request, found, deps, s, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "POST", "/admin/runs/retry", "admin")
def runs_retry(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    if not _can_retry(deps, s):
        return _runs_page(request, found, deps, s, error=NOTHING_TO_RETRY, status_code=422)
    reruns.request(s, found.user.id, deps.now())
    return _redirect("/admin/runs", "retry_requested")


@route(router, "POST", "/admin/outbox/requeue", "admin")
def outbox_requeue(found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    rows = outbox.requeue_failed(s, None, deps.now())
    repo.add_audit(
        s, None, "outbox_requeue", "outbox", {"count": len(rows)}, web_actor(found.user.id)
    )
    return _redirect("/admin/runs", "outbox_requeued")
