"""管理员页面：邀请码、使用者启停、运行记录、立即重试、失败邮件重新排队。

路由全部是 access="admin"，非管理员一律 404。本模块只解析表单、调用服务、显示服务给出的中文原因；
页面里只有账号层面的信息，不读取、不显示任何使用者的账本、现金、持仓、信号或日志内容（设计 6.7）。
"""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from qt.db import repo
from qt.mail import outbox
from qt.service import auth, reruns, views
from qt.service.actor import web_actor
from qt.service.errors import AuthError
from qt.service.views import admin as admin_views
from qt.web.app import route
from qt.web.deps import FLASH_MESSAGES, Current, CurrentUser, DbSession, Deps, WebDeps, render

router = APIRouter()

FormText = Annotated[str, Form()]
NOTE_MAX_LENGTH = 200
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
    if len(note.strip()) > NOTE_MAX_LENGTH:
        return _invites_page(
            request,
            found,
            deps,
            s,
            error=f"备注不能超过 {NOTE_MAX_LENGTH} 个字",
            note=note,
            status_code=422,
        )
    auth.create_invite(s, found.user.id, note, deps.cfg.web.invite_valid_days, deps.now())
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


# ---- 运行记录与重试 ----


def _runs_page(
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
        "section": "runs",
        "view": views.recent_runs(s, RUNS_SHOWN),
        "rerun": reruns.status_text(reruns.latest(s)),
        "flash": flash,
        "error": error,
    }
    return render(request, "admin_runs.html", context, found=found, status_code=status_code)


@route(router, "GET", "/admin/runs", "admin")
def runs_page(
    request: Request, found: CurrentUser, s: DbSession, msg: str | None = None
) -> Response:
    return _runs_page(request, found, s, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "POST", "/admin/runs/retry", "admin")
def runs_retry(request: Request, found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    if not reruns.has_unfinished_day(s, deps.calendar(), deps.now(), deps.cfg.service.run_time):
        return _runs_page(request, found, s, error=NOTHING_TO_RETRY, status_code=422)
    reruns.request(s, found.user.id, deps.now())
    return _redirect("/admin/runs", "retry_requested")


@route(router, "POST", "/admin/outbox/requeue", "admin")
def outbox_requeue(found: CurrentUser, deps: Deps, s: DbSession) -> Response:
    rows = outbox.requeue_failed(s, None, deps.now())
    repo.add_audit(
        s, None, "outbox_requeue", "outbox", {"count": len(rows)}, web_actor(found.user.id)
    )
    return _redirect("/admin/runs", "outbox_requeued")
