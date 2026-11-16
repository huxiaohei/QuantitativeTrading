"""网页应用：路由登记表、应用工厂、错误页、后台补发账号邮件与每日清理。

所有路由都通过 route() 登记，并标明"公开 / 需登录 / 需管理员"三类之一；
需登录、需管理员的路由自动挂上对应的依赖，所有 POST 自动挂上防伪造检查。
登记表 ROUTES 在路由模块被导入时填好（每条路由一项），create_app 只是把路由挂到应用上。
"""

import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.params import Depends as DependsParam
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from qt.mail import outbox
from qt.mail.account import ACCOUNT_KINDS
from qt.service import auth
from qt.service.errors import NotFoundError
from qt.web.deps import (
    STATIC_DIR,
    RedirectError,
    WebDeps,
    check_public_csrf,
    check_session_csrf,
    current,
    require_admin,
)
from qt.web.security import SecurityMiddleware, error_page

Access = Literal["public", "user", "admin"]
Method = Literal["GET", "POST"]
# 发送失败的账号邮件的补发间隔（秒）
RESEND_INTERVAL_SECONDS = 120

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteInfo:
    """一条路由的登记信息。"""

    method: Method
    path: str
    access: Access
    name: str


ROUTES: list[RouteInfo] = []

Endpoint = Callable[..., Any]


def route(
    router: APIRouter, method: Method, path: str, access: Access
) -> Callable[[Endpoint], Endpoint]:
    """登记并挂上一条路由（装饰器）。"""

    def decorator(func: Endpoint) -> Endpoint:
        dependencies: list[DependsParam] = []
        if access == "user":
            dependencies.append(Depends(current))
        elif access == "admin":
            dependencies.append(Depends(require_admin))
        if method == "POST":
            check = check_public_csrf if access == "public" else check_session_csrf
            dependencies.append(Depends(check))
        router.add_api_route(
            path,
            func,
            methods=[method],
            dependencies=dependencies,
            response_class=HTMLResponse,
            include_in_schema=False,
        )
        ROUTES.append(RouteInfo(method, path, access, func.__name__))
        return func

    return decorator


# ---- 后台：补发账号邮件与每日清理 ----


def send_account_mail(deps: WebDeps, outbox_id: int) -> None:
    """请求提交后立即发这一封账号邮件；失败留在发件箱里由后台线程补发。"""
    try:
        with Session(deps.engine) as s:
            outbox.send_one(s, deps.sender, outbox_id, deps.now())
    except Exception:
        _log.exception("账号邮件 #%s 立即发送出错，稍后补发", outbox_id)


def run_background_once(deps: WebDeps, last_purge: date | None) -> date | None:
    """补发一轮账号邮件；当天还没清理过则清理一次过期数据。返回最近清理的日期。"""
    now = deps.now()
    with Session(deps.engine) as s:
        outbox.send_pending(s, deps.sender, now, kinds=ACCOUNT_KINDS)
        if last_purge != now.date():
            removed = auth.purge_expired(s, now)
            s.commit()
            _log.info("已清理过期的会话、令牌与频率记录 %s 条", removed)
            return now.date()
    return last_purge


def _background_loop(deps: WebDeps, stop: threading.Event) -> None:
    last_purge: date | None = None
    while not stop.is_set():
        try:
            last_purge = run_background_once(deps, last_purge)
        except Exception:
            _log.exception("后台补发账号邮件或清理出错，下一轮重试")
        stop.wait(RESEND_INTERVAL_SECONDS)


# ---- 应用 ----


async def _redirect(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RedirectError)
    return RedirectResponse(exc.url, status_code=303)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    return HTMLResponse(error_page(exc.status_code), status_code=exc.status_code)


async def _not_found(request: Request, exc: Exception) -> Response:
    """服务层取不到本人名下的记录（含别人的编号）：一律 404，不区分"不存在"与"不是你的"。"""
    return HTMLResponse(error_page(404), status_code=404)


async def _validation_error(request: Request, exc: Exception) -> Response:
    return HTMLResponse(error_page(400), status_code=400)


def create_app(deps: WebDeps) -> FastAPI:
    """按运行环境建应用。deps.background 为真时启动后台补发与清理线程，应用关闭时退出。"""
    from qt.web.routes import ROUTERS

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        stop = threading.Event()
        worker: threading.Thread | None = None
        if deps.background:
            worker = threading.Thread(
                target=_background_loop, args=(deps, stop), name="qt-web-background", daemon=True
            )
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=30)

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, title="量化信号"
    )
    app.state.deps = deps
    # 逐条挂到应用上，而不是 include_router：新版 FastAPI 把包含的路由器包成一个延迟展开的
    # 整体，app.routes 里看不到单条路由，路由登记测试就无法逐条核对
    for router in ROUTERS:
        for item in router.routes:
            assert isinstance(item, APIRoute) and item.methods is not None
            app.add_api_route(
                item.path,
                item.endpoint,
                methods=sorted(item.methods),
                dependencies=item.dependencies,
                response_class=HTMLResponse,
                include_in_schema=False,
                name=item.name,
            )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(RedirectError, _redirect)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(NotFoundError, _not_found)
    app.add_middleware(SecurityMiddleware, deps=deps)
    return app
