"""安全中间件：统一的安全响应头、防伪造 Cookie、请求体大小上限、500 错误页。

放在所有路由之外：任何响应（含错误页、静态文件）都经过这里加头；
路由里没接住的异常在这里记日志并换成不含任何异常信息的中文 500 页面。
"""

import logging
import secrets

from fastapi import HTTPException
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from qt.web.deps import TEMPLATES, WebDeps, cookie_header

MAX_BODY_BYTES = 64 * 1024
CSP = "default-src 'self'; frame-ancestors 'none'"
_HSTS = "max-age=31536000"
_STATIC_PREFIX = "/static/"
_CSRF_COOKIE_MAX_AGE = 30 * 86400

_log = logging.getLogger(__name__)

ERROR_TEXT: dict[int, tuple[str, str]] = {
    400: ("请求有误", "提交的内容无法识别，请返回上一页重新填写。"),
    403: ("请求无效", "请求无效（可能是页面已过期），请返回上一页刷新后重试。"),
    404: ("页面不存在", "页面不存在，或者你没有权限查看。"),
    405: ("请求有误", "不支持这种访问方式。"),
    413: ("内容过大", "提交的内容过大，请精简后重试。"),
    500: ("服务出错", "服务出错了，请稍后再试。问题已记录。"),
}


def error_page(status: int) -> bytes:
    """渲染中文错误页；只显示固定文字，不含任何异常信息。"""
    title, message = ERROR_TEXT.get(status, ERROR_TEXT[400] if status < 500 else ERROR_TEXT[500])
    return (
        TEMPLATES.get_template("error.html")
        .render(status=status, title=title, message=message, csrf="", user=None)
        .encode()
    )


class SecurityMiddleware:
    """纯 ASGI 中间件（不缓冲响应体，不影响后台任务）。"""

    def __init__(self, app: ASGIApp, deps: WebDeps) -> None:
        self.app = app
        self.deps = deps

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        deps = self.deps
        conn = HTTPConnection(scope)
        path = conn.url.path
        is_static = path.startswith(_STATIC_PREFIX)
        state = conn.state
        sent_csrf = conn.cookies.get(deps.csrf_cookie)
        new_csrf = None if sent_csrf else secrets.token_urlsafe(32)
        state.csrf_cookie = sent_csrf or new_csrf
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                if deps.secure:
                    headers["Strict-Transport-Security"] = _HSTS
                if not is_static:
                    headers["Cache-Control"] = "no-store"
                    if new_csrf is not None:
                        headers.append(
                            "Set-Cookie",
                            cookie_header(
                                deps.csrf_cookie,
                                new_csrf,
                                max_age=_CSRF_COOKIE_MAX_AGE,
                                secure=deps.secure,
                            ),
                        )
                    # 响应自己已经设置了会话 Cookie（登录、退出、以后的改密码等）时以它为准，
                    # 不再追加顺延：浏览器按顺序处理，后一条会把清掉的旧令牌又写回去
                    refresh = getattr(state, "refresh_session", None)
                    prefix = deps.session_cookie + "="
                    sets_session = any(
                        value.startswith(prefix) for value in headers.getlist("set-cookie")
                    )
                    if isinstance(refresh, str) and not sets_session:
                        headers.append("Set-Cookie", refresh)
            await send(message)

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise HTTPException(413)
            return message

        length = conn.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            await self._send_error(send_wrapper, 413)
            return
        try:
            await self.app(scope, limited_receive, send_wrapper)
        except Exception:
            # 只记方法与路径，不记查询串（验证、重置链接的令牌在查询串里）
            _log.exception("请求处理出错：%s %s", scope.get("method"), path)
            if started:
                raise
            await self._send_error(send_wrapper, 500)

    @staticmethod
    async def _send_error(send: Send, status: int) -> None:
        body = error_page(status)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
