"""系统账页（只读）：按信号自动执行的对照账本的净值、持仓与流水。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from qt.service import views
from qt.web.app import route
from qt.web.deps import CurrentUser, DbSession, Deps, WebDeps, render
from qt.web.routes.common import SubParam, live_context, page_context, stock_names

router = APIRouter()


@route(router, "GET", "/system", "user")
def system(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    shown, context = page_context(s, found, sub, "system", "/system")
    view = None
    if shown is not None:
        view = _book(deps, s, found.user.id, shown.id)
        context.update(live_context(deps, view.holdings, _holdings_url(sub), levels=False))
    return render(request, "system.html", {**context, "view": view}, found=found)


def _book(deps: WebDeps, s: Session, user_id: int, subscription_id: int) -> views.SystemBookView:
    market = deps.market(deps.now().date(), views.book_symbols(s, user_id, subscription_id))
    return views.system_book(s, user_id, subscription_id, market, names=stock_names(deps))


def _holdings_url(sub: int | None) -> str:
    return "/system/holdings" if sub is None else f"/system/holdings?sub={sub}"


@route(router, "GET", "/system/holdings", "user")
def system_holdings(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    """只返回持仓表这一块，供页面在交易时段定时替换。订阅归属与 /system 相同。"""
    shown = views.resolve_subscription(s, found.user.id, sub)
    rows = [] if shown is None else _book(deps, s, found.user.id, shown.id).holdings
    context = live_context(deps, rows, _holdings_url(sub), levels=False)
    return render(request, "_holdings_live.html", context, found=found)
