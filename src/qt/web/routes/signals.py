"""信号页：待处理的信号、历史信号及状态、被否决的候选及原因。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from qt.service import views
from qt.web.app import route
from qt.web.deps import CurrentUser, DbSession, Deps, render
from qt.web.routes.common import SubParam, page_context, stock_names

router = APIRouter()


@route(router, "GET", "/signals", "user")
def signals(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    shown, context = page_context(s, found, sub, "signals", "/signals")
    view = None
    if shown is not None:
        view = views.signals_page(
            s,
            found.user.id,
            shown.id,
            deps.calendar(),
            deps.cfg,
            deps.now().date(),
            names=stock_names(deps),
        )
    return render(request, "signals.html", {**context, "view": view}, found=found)
