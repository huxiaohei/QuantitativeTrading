"""公开页面：首页跳转、登录、注册、验证邮箱、重发验证、找回与重置密码、条款、退出、健康检查。

页面上看不出某个邮箱是否注册过：注册、重发验证、找回密码在各种情况下显示同一段文字。
账号服务遇到 AuthError 时已经记下频率与审计，这里接住后正常返回，由请求的会话提交。
账号邮件在事务提交之后由后台任务立即发送。
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from qt.db import admin_repo, repo
from qt.db.models import User
from qt.mail.account import (
    queue_account_mail,
    render_password_changed,
    render_register_attempt,
    render_reset,
    render_verify,
)
from qt.mail.render import Mail
from qt.service import auth
from qt.service.actor import web_actor
from qt.service.errors import AuthError, EmailNotVerifiedError, RateLimitedError
from qt.web.app import route, send_account_mail
from qt.web.deps import (
    FLASH_MESSAGES,
    CurrentUser,
    DbSession,
    Deps,
    OptionalUser,
    WebDeps,
    client_ip,
    cookie_header,
    render,
    safe_next,
)
from qt.web.forms import FormError, parse_bool, parse_password

router = APIRouter()

RESEND_SENT = "如果该邮箱已注册且尚未验证，验证邮件已重新发出。"
FORGOT_SENT = "如果该邮箱已注册，重置邮件已发出。请查收邮件并按说明设置新密码。"
REGISTER_SENT = "我们已向你填写的邮箱发出一封邮件，请查收并按邮件里的说明操作。"
NOT_VERIFIED = "请先验证邮箱"

FormText = Annotated[str, Form()]


def _queue_mail(
    s: Session, tasks: BackgroundTasks, deps: WebDeps, user: User, kind: str, mail: Mail
) -> None:
    """账号邮件入队；请求的事务提交后由后台任务立即发送。"""
    outbox_id = queue_account_mail(s, user, kind, mail, deps.now())
    if outbox_id is not None:
        tasks.add_task(send_account_mail, deps, outbox_id)


def _invalid_link(request: Request, template: str) -> Response:
    return render(request, template, {"valid": False}, status_code=410)


# ---- 首页与健康检查 ----


@route(router, "GET", "/", "public")
def home(found: OptionalUser) -> Response:
    return RedirectResponse("/overview" if found is not None else "/login", status_code=303)


@route(router, "GET", "/healthz", "public")
def healthz() -> Response:
    """存活检查：不查数据库。"""
    return PlainTextResponse("ok")


# ---- 登录与退出 ----


def _login_page(
    request: Request,
    *,
    email: str = "",
    next_url: str | None = None,
    flash: str | None = None,
    error: str | None = None,
    unverified: bool = False,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "email": email,
        "next": safe_next(next_url) or "",
        "flash": flash,
        "error": error,
        "unverified": unverified,
    }
    return render(request, "login.html", context, status_code=status_code)


@route(router, "GET", "/login", "public")
def login_page(
    request: Request,
    next_url: Annotated[str | None, Query(alias="next")] = None,
    msg: str | None = None,
) -> Response:
    return _login_page(request, next_url=next_url, flash=FLASH_MESSAGES.get(msg or ""))


@route(router, "POST", "/login", "public")
def login_submit(
    request: Request,
    deps: Deps,
    s: DbSession,
    email: FormText = "",
    password: FormText = "",
    next_url: Annotated[str, Form(alias="next")] = "",
) -> Response:
    try:
        parse_password(password, "password")
        result = auth.login(
            s,
            email,
            password,
            client_ip(request),
            request.headers.get("user-agent", ""),
            deps.cfg.web,
            deps.now(),
        )
    except FormError as exc:
        return _login_page(
            request, email=email, next_url=next_url, error=exc.message, status_code=400
        )
    except EmailNotVerifiedError:
        return _login_page(
            request,
            email=email,
            next_url=next_url,
            error=NOT_VERIFIED,
            unverified=True,
            status_code=400,
        )
    except AuthError as exc:
        return _login_page(request, email=email, next_url=next_url, error=str(exc), status_code=400)
    response = RedirectResponse(safe_next(next_url) or "/overview", status_code=303)
    response.headers.append(
        "Set-Cookie",
        cookie_header(
            deps.session_cookie, result.token, max_age=deps.session_max_age, secure=deps.secure
        ),
    )
    return response


@route(router, "POST", "/logout", "user")
def logout(request: Request, deps: Deps, s: DbSession) -> Response:
    token = request.cookies.get(deps.session_cookie)
    if token:
        auth.logout(s, token)
    response = RedirectResponse("/login", status_code=303)
    response.headers.append(
        "Set-Cookie", cookie_header(deps.session_cookie, "", max_age=0, secure=deps.secure)
    )
    return response


# ---- 注册 ----


def _register_page(
    request: Request,
    deps: WebDeps,
    *,
    email: str = "",
    invite: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, object] = {
        "email": email,
        "invite": invite,
        "error": error,
        "invite_required": deps.cfg.web.signup_mode == "invite",
        "min_length": deps.cfg.web.password_min_length,
    }
    return render(request, "register.html", context, status_code=status_code)


@route(router, "GET", "/register", "public")
def register_page(request: Request, deps: Deps) -> Response:
    return _register_page(request, deps)


@route(router, "POST", "/register", "public")
def register_submit(
    request: Request,
    deps: Deps,
    s: DbSession,
    tasks: BackgroundTasks,
    email: FormText = "",
    password: FormText = "",
    invite: FormText = "",
    accept_terms: Annotated[str | None, Form()] = None,
) -> Response:
    now = deps.now()
    try:
        parse_password(password, "password")
        result = auth.register(
            s,
            email,
            password,
            invite,
            parse_bool(accept_terms),
            deps.cfg.web,
            client_ip(request),
            now,
        )
    except (FormError, AuthError) as exc:
        message = exc.message if isinstance(exc, FormError) else str(exc)
        return _register_page(
            request, deps, email=email, invite=invite, error=message, status_code=400
        )
    user = repo.get_user(s, result.user_id)
    if result.kind == "created":
        assert result.token is not None
        mail = render_verify(deps.base_url, result.token, deps.cfg.web.verify_token_hours)
        _queue_mail(s, tasks, deps, user, "account_verify", mail)
    else:
        mail = render_register_attempt(deps.base_url)
        _queue_mail(s, tasks, deps, user, "account_register_attempt", mail)
    # 两种结果跳到同一页、不登录、不下发会话 Cookie，页面上看不出邮箱是否已注册
    return RedirectResponse("/register/sent", status_code=303)


@route(router, "GET", "/register/sent", "public")
def register_sent(request: Request) -> Response:
    return render(request, "check_mail.html", {"message": REGISTER_SENT})


# ---- 验证邮箱 ----


@route(router, "GET", "/verify", "public")
def verify_page(request: Request, deps: Deps, s: DbSession, token: str = "") -> Response:
    """只查看令牌是否有效，不消耗：邮箱的链接预览、杀毒扫描打开链接不会把它用掉。"""
    if auth.peek_email_token(s, token, "verify", deps.now()) is None:
        return _invalid_link(request, "verify.html")
    return render(request, "verify.html", {"valid": True, "token": token})


@route(router, "POST", "/verify", "public")
def verify_submit(request: Request, deps: Deps, s: DbSession, token: FormText = "") -> Response:
    if auth.verify_email(s, token, deps.now()) is None:
        return _invalid_link(request, "verify.html")
    return RedirectResponse("/login?msg=verified", status_code=303)


@route(router, "POST", "/verify/resend", "public")
def verify_resend(
    request: Request, deps: Deps, s: DbSession, tasks: BackgroundTasks, email: FormText = ""
) -> Response:
    """无论邮箱是否存在、是否已验证、是否超频，都显示同一句话。"""
    now = deps.now()
    user = admin_repo.find_user_by_email(s, auth.normalize_email(email))
    if user is not None and user.email_verified_at is None:
        try:
            token = auth.resend_verification(s, user.id, deps.cfg.web, now)
        except RateLimitedError:
            token = None
        if token is not None:
            mail = render_verify(deps.base_url, token, deps.cfg.web.verify_token_hours)
            _queue_mail(s, tasks, deps, user, "account_verify", mail)
    return render(request, "check_mail.html", {"message": RESEND_SENT})


# ---- 找回与重置密码 ----


@route(router, "GET", "/forgot", "public")
def forgot_page(request: Request) -> Response:
    return render(request, "forgot.html", {"sent": False})


@route(router, "POST", "/forgot", "public")
def forgot_submit(
    request: Request, deps: Deps, s: DbSession, tasks: BackgroundTasks, email: FormText = ""
) -> Response:
    """无论邮箱是否存在，都显示同一句话。"""
    found = auth.request_password_reset(s, email, deps.cfg.web, deps.now())
    if found is not None:
        user, token = found
        mail = render_reset(deps.base_url, token, deps.cfg.web.reset_token_hours)
        _queue_mail(s, tasks, deps, user, "account_reset", mail)
    return render(request, "forgot.html", {"sent": True, "message": FORGOT_SENT})


def _reset_form(
    request: Request, deps: WebDeps, token: str, error: str | None, status_code: int
) -> Response:
    context: dict[str, object] = {
        "valid": True,
        "token": token,
        "error": error,
        "min_length": deps.cfg.web.password_min_length,
    }
    return render(request, "reset.html", context, status_code=status_code)


@route(router, "GET", "/reset", "public")
def reset_page(request: Request, deps: Deps, s: DbSession, token: str = "") -> Response:
    if auth.peek_email_token(s, token, "reset", deps.now()) is None:
        return _invalid_link(request, "reset.html")
    return _reset_form(request, deps, token, None, 200)


@route(router, "POST", "/reset", "public")
def reset_submit(
    request: Request,
    deps: Deps,
    s: DbSession,
    tasks: BackgroundTasks,
    token: FormText = "",
    password: FormText = "",
) -> Response:
    now = deps.now()
    if auth.peek_email_token(s, token, "reset", now) is None:
        return _invalid_link(request, "reset.html")
    try:
        parse_password(password, "password")
        user = auth.reset_password(s, token, password, deps.cfg.web, now)
    except FormError as exc:
        return _reset_form(request, deps, token, exc.message, 400)
    except AuthError as exc:
        return _reset_form(request, deps, token, str(exc), 400)
    _queue_mail(
        s,
        tasks,
        deps,
        user,
        "account_password_changed",
        render_password_changed(deps.base_url, now),
    )
    return RedirectResponse("/login?msg=reset", status_code=303)


# ---- 条款 ----


@route(router, "GET", "/terms", "public")
def terms(request: Request) -> Response:
    return render(request, "terms.html")


def _terms_accept_page(
    request: Request, found: CurrentUser, error: str | None, status_code: int
) -> Response:
    return render(
        request, "terms_accept.html", {"error": error}, found=found, status_code=status_code
    )


@route(router, "GET", "/terms/accept", "user")
def terms_accept_page(request: Request, deps: Deps, found: CurrentUser) -> Response:
    if not auth.needs_terms(found.user, deps.cfg.web):
        return RedirectResponse("/overview", status_code=303)
    return _terms_accept_page(request, found, None, 200)


@route(router, "POST", "/terms/accept", "user")
def terms_accept_submit(
    request: Request,
    deps: Deps,
    s: DbSession,
    found: CurrentUser,
    accept: Annotated[str | None, Form()] = None,
) -> Response:
    if not parse_bool(accept):
        return _terms_accept_page(request, found, "请先勾选同意，才能继续使用。", 400)
    user_id = found.user.id
    auth.accept_terms(s, user_id, deps.cfg.web, deps.now(), web_actor(user_id))
    return RedirectResponse("/overview", status_code=303)
