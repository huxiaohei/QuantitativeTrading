"""网页的依赖：运行环境、每请求一个数据库会话、当前使用者、防伪造检查、Cookie、来访 IP、模板渲染。

当前使用者只从会话 Cookie 得出，绝不从网址、表单、请求头读 user_id。
"""

import hmac
import http.cookies
import ipaddress
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import jinja2
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.store import ParquetStore
from qt.db.models import User, WebSession
from qt.mail.send import Sender
from qt.runtime import cookie_secure
from qt.service import auth
from qt.service.market import StoreMarket

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)


def _money(value: float | None) -> str:
    """金额：两位小数，千分位；没有值显示 -。"""
    return "-" if value is None else f"{value:,.2f}"


def _price(value: float | None) -> str:
    """价格：两位小数；没有值显示 -。"""
    return "-" if value is None else f"{value:.2f}"


def _shares(value: float) -> str:
    """股数：整数，千分位。"""
    return f"{value:,.0f}"


def _pct(value: float | None) -> str:
    """比例（0.05 → 5.00%）；没有值显示 -。"""
    return "-" if value is None else f"{value * 100:.2f}%"


def _trend(value: float | None) -> str:
    """涨跌的样式名：涨为 up（红），跌为 down（绿），其余为空。"""
    if value is None or value == 0:
        return ""
    return "up" if value > 0 else "down"


TEMPLATES.filters.update(money=_money, price=_price, shares=_shares, pct=_pct, trend=_trend)

# 已登录但条款版本过期时，只放行这两个地址
_TERMS_EXEMPT = frozenset({"/terms/accept", "/logout"})
# 跳转后显示的提示：网址里只带代号，文字取自这张表，绝不显示网址里的任意文字
FLASH_MESSAGES: dict[str, str] = {
    "verified": "邮箱已验证，请登录",
    "expired": "登录已过期，请重新登录",
    "reset": "密码已重设，请用新密码登录",
    "sub_created": "订阅已创建",
    "params_saved": "参数已修改，从生效日起按新参数计算",
    "sub_paused": "订阅已暂停",
    "sub_resumed": "订阅已恢复",
    "sub_closed": "订阅已关闭",
    "manual_trade": "已录入成交",
    "manual_holding": "已录入已有持仓",
    "manual_deposit": "已录入入金",
    "manual_withdraw": "已录入出金",
    "manual_void": "已作废流水",
    "manual_duplicate": "这笔已经录入过，没有重复记录",
    "void_na": "这条流水不能作废（已经作废，或本身是作废记录）",
}


@dataclass(frozen=True)
class WebDeps:
    """网页进程的运行环境。测试直接构造；qt web 由 Runtime 构造。"""

    engine: Engine
    cfg: AppConfig
    base_url: str
    sender: Sender
    now: Callable[[], datetime]
    store: ParquetStore
    calendar: Callable[[], TradingCalendar]
    # (今天, 账本涉及的股票集合) -> 行情；面板只覆盖这些股票，由 MarketCache.get 提供
    market: Callable[[date, Collection[str]], StoreMarket]
    background: bool = True  # 是否启动补发账号邮件、每日清理的后台线程（测试里关掉）
    secure: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "secure", cookie_secure(self.base_url))

    @property
    def origin(self) -> str:
        """base_url 的源（协议 + 主机 + 端口），用于比对 Origin 头。"""
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    def cookie_name(self, base: str) -> str:
        """https 下加 __Host- 前缀；浏览器要求带该前缀的 Cookie 必须有 Secure，http 下去掉。"""
        return f"__Host-{base}" if self.secure else base

    @property
    def session_cookie(self) -> str:
        return self.cookie_name("qt_session")

    @property
    def csrf_cookie(self) -> str:
        return self.cookie_name("qt_csrf")

    @property
    def session_max_age(self) -> int:
        return self.cfg.web.session_days * 86400


@dataclass(frozen=True)
class Current:
    """当前登录的使用者与会话。"""

    user: User
    session: WebSession


class RedirectError(Exception):
    """依赖里需要跳转（未登录、需同意条款）时抛出，由应用转成 303 跳转。"""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def cookie_header(name: str, value: str, *, max_age: int, secure: bool) -> str:
    """Set-Cookie 头的值：HttpOnly、SameSite=Lax、Path=/，不带 Domain。"""
    jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
    jar[name] = value
    morsel = jar[name]
    morsel["max-age"] = max_age
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "lax"
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def get_deps(request: Request) -> WebDeps:
    deps = request.app.state.deps
    assert isinstance(deps, WebDeps)
    return deps


Deps = Annotated[WebDeps, Depends(get_deps)]


def db_session(deps: Deps) -> Iterator[Session]:
    """每请求一个会话：路由正常结束（含路由自己接住 AuthError 后返回）时提交，出异常时回滚。

    作用域为 function：在发送响应与运行后台任务之前提交，后台发信时数据已入库。
    """
    s = Session(deps.engine, expire_on_commit=False)
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


DbSession = Annotated[Session, Depends(db_session, scope="function")]


def client_ip(request: Request) -> str:
    """来访 IP：直接连接方是私有网段（即反向代理）时取 X-Forwarded-For 的最后一段，

    否则用连接方地址（公网直连时不信任该头，防止伪造）。
    """
    direct = request.client.host if request.client is not None else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and _is_private(direct):
        last = forwarded.split(",")[-1].strip()
        if _is_ip(last):
            return last
    return direct


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _is_private(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).is_private
    except ValueError:
        return False


def safe_next(raw: str | None) -> str | None:
    """登录后的去处：只接受以单个 / 开头的站内路径，其余一律返回 None。"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    return raw


def _session_token(request: Request, deps: WebDeps) -> str | None:
    return request.cookies.get(deps.session_cookie) or None


def load_current(request: Request, deps: WebDeps, s: Session) -> Current | None:
    """按会话 Cookie 取当前使用者；没有有效会话返回 None。会话被顺延时安排刷新 Cookie。"""
    token = _session_token(request, deps)
    if token is None:
        return None
    now = deps.now()
    loaded = auth.load_session(s, token, deps.cfg.web, now)
    if loaded is None:
        return None
    session, user = loaded
    if session.last_seen_at == now:  # 本次刚顺延过有效期：Cookie 也一起顺延
        request.state.refresh_session = cookie_header(
            deps.session_cookie, token, max_age=deps.session_max_age, secure=deps.secure
        )
    return Current(user, session)


def optional_current(request: Request, deps: Deps, s: DbSession) -> Current | None:
    """公开页用：已登录返回当前使用者，否则 None，不跳转。"""
    return load_current(request, deps, s)


def current(request: Request, deps: Deps, s: DbSession) -> Current:
    """需登录的页面：没有有效会话时 GET 跳到登录页（带回跳地址），POST 跳到登录页并提示已过期。

    已登录但条款版本过期时，除同意条款与退出外一律跳到 /terms/accept。
    """
    found = load_current(request, deps, s)
    if found is None:
        if request.method == "GET":
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query
            raise RedirectError("/login?next=" + quote(path, safe=""))
        raise RedirectError("/login?msg=expired")
    if request.url.path not in _TERMS_EXEMPT and auth.needs_terms(found.user, deps.cfg.web):
        raise RedirectError("/terms/accept")
    return found


CurrentUser = Annotated[Current, Depends(current)]
OptionalUser = Annotated[Current | None, Depends(optional_current)]


def require_admin(found: CurrentUser) -> Current:
    """需管理员的页面：不是管理员一律 404（不泄露页面存在）。"""
    if not found.user.is_admin:
        raise HTTPException(404)
    return found


# ---- 防伪造 ----


def _forbidden() -> HTTPException:
    return HTTPException(403)


def _check_origin(request: Request, deps: WebDeps) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin.strip().lower() != deps.origin:
        raise _forbidden()


async def _form_csrf(request: Request) -> str:
    value = (await request.form()).get("csrf")
    return value if isinstance(value, str) else ""


def _same(expected: str | None, given: str) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


async def check_public_csrf(request: Request, deps: Deps) -> None:
    """公开页的 POST：表单 csrf 必须等于防伪造 Cookie（双重提交），并检查 Origin。"""
    _check_origin(request, deps)
    if not _same(request.cookies.get(deps.csrf_cookie), await _form_csrf(request)):
        raise _forbidden()


async def check_session_csrf(request: Request, deps: Deps, found: CurrentUser) -> None:
    """登录后的 POST：表单 csrf 必须等于会话里的防伪造令牌，并检查 Origin。"""
    _check_origin(request, deps)
    if not _same(found.session.csrf_token, await _form_csrf(request)):
        raise _forbidden()


# ---- 模板 ----


def render(
    request: Request,
    name: str,
    context: dict[str, object] | None = None,
    *,
    found: Current | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """渲染页面。已登录的页面传 found：表单用会话的防伪造令牌，导航按是否管理员显示。"""
    deps = get_deps(request)
    csrf = found.session.csrf_token if found is not None else str(request.state.csrf_cookie)
    page = TEMPLATES.get_template(name).render(
        csrf=csrf,
        user=None if found is None else found.user,
        terms_version=deps.cfg.web.terms_version,
        **(context or {}),
    )
    return HTMLResponse(page, status_code=status_code)
