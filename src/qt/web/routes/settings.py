"""设置页面：邮件开关、修改密码、退出所有设备。

本模块只解析表单、调用服务、显示服务给出的中文原因；密码规则、会话处理都在 qt.service.auth。
账号服务遇到 AuthError 时已经记下频率与审计，这里接住后正常返回，由请求的会话提交。
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from qt.mail.account import queue_account_mail, render_password_changed
from qt.service import accounts, auth
from qt.service.actor import web_actor
from qt.service.errors import AuthError
from qt.web.app import route, send_account_mail
from qt.web.deps import (
    FLASH_MESSAGES,
    Current,
    CurrentUser,
    DbSession,
    Deps,
    WebDeps,
    cookie_header,
    render,
)
from qt.web.forms import FormError, parse_bool, parse_password

router = APIRouter()

FormText = Annotated[str, Form()]


def _page(
    request: Request,
    found: Current,
    *,
    flash: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "active": "settings",
        "weekly_mail": found.user.weekly_mail,
        "abandon_mail": found.user.abandon_mail,
        "flash": flash,
        "error": error,
    }
    return render(request, "settings.html", context, found=found, status_code=status_code)


@route(router, "GET", "/settings", "user")
def settings_page(request: Request, found: CurrentUser, msg: str | None = None) -> Response:
    return _page(request, found, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "POST", "/settings/mail", "user")
def settings_mail(
    s: DbSession,
    found: CurrentUser,
    weekly_mail: Annotated[str | None, Form()] = None,
    abandon_mail: Annotated[str | None, Form()] = None,
) -> Response:
    """两个开关一起提交：没勾上的不会出现在表单里，即关闭。"""
    actor = web_actor(found.user.id)
    accounts.set_weekly_mail(s, found.user.id, parse_bool(weekly_mail), actor=actor)
    accounts.set_abandon_mail(s, found.user.id, parse_bool(abandon_mail), actor=actor)
    return RedirectResponse("/settings?msg=settings_saved", status_code=303)


def _queue_changed_mail(s: Session, tasks: BackgroundTasks, deps: WebDeps, found: Current) -> None:
    """ "密码已修改"通知入队；提交之后由后台任务立即发送。deps.now() 是上海时间。"""
    now = deps.now()
    outbox_id = queue_account_mail(
        s,
        found.user,
        "account_password_changed",
        render_password_changed(deps.base_url, now, kept_current=True),
        now,
    )
    if outbox_id is not None:
        tasks.add_task(send_account_mail, deps, outbox_id)


@route(router, "POST", "/settings/password", "user")
def settings_password(
    request: Request,
    deps: Deps,
    s: DbSession,
    found: CurrentUser,
    tasks: BackgroundTasks,
    current: FormText = "",
    new: FormText = "",
) -> Response:
    try:
        parse_password(current, "current")
        parse_password(new, "new")
        auth.change_password(
            s, found.user.id, current, new, deps.cfg.web, found.session.id, deps.now()
        )
    except FormError as exc:
        return _page(request, found, error=exc.message, status_code=422)
    except AuthError as exc:
        return _page(request, found, error=str(exc), status_code=422)
    _queue_changed_mail(s, tasks, deps, found)
    # 当前会话保留，Cookie 不变
    return RedirectResponse("/settings?msg=password_changed", status_code=303)


@route(router, "POST", "/settings/logout-all", "user")
def settings_logout_all(deps: Deps, s: DbSession, found: CurrentUser) -> Response:
    """删除全部会话（含当前这个）：清掉当前浏览器的 Cookie，回到登录页。"""
    auth.logout_all(s, found.user.id)
    response = RedirectResponse("/login?msg=logout_all", status_code=303)
    response.headers.append(
        "Set-Cookie", cookie_header(deps.session_cookie, "", max_age=0, secure=deps.secure)
    )
    return response
