"""系统账页（只读）：按信号自动执行的对照账本的净值、持仓与流水。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from qt.service import views
from qt.web.app import route
from qt.web.deps import CurrentUser, DbSession, Deps, render
from qt.web.routes.common import SubParam, page_context, stock_names

router = APIRouter()


@route(router, "GET", "/system", "user")
def system(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    shown, context = page_context(s, found, sub, "system", "/system")
    view = None
    if shown is not None:
        user_id = found.user.id
        market = deps.market(deps.now().date(), views.book_symbols(s, user_id, shown.id))
        view = views.system_book(s, user_id, shown.id, market, names=stock_names(deps))
    return render(request, "system.html", {**context, "view": view}, found=found)
