"""总览页：最近一次运行、今天的信号、两本账的净值、现金、持仓市值。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from qt.service import views
from qt.web.app import route
from qt.web.deps import CurrentUser, DbSession, Deps, render
from qt.web.routes.common import SubParam, page_context, stock_names

router = APIRouter()


@route(router, "GET", "/overview", "user")
def overview(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    shown, context = page_context(s, found, sub, "overview", "/overview")
    user_id = found.user.id
    today = deps.now().date()
    if shown is None:
        view = views.overview(s, user_id, None, deps.calendar(), deps.cfg, today)
    else:
        market = deps.market(today, views.book_symbols(s, user_id, shown.id))
        view = views.overview(
            s,
            user_id,
            market,
            deps.calendar(),
            deps.cfg,
            today,
            subscription_id=shown.id,
            names=stock_names(deps),
        )
    return render(request, "overview.html", {**context, "view": view}, found=found)
