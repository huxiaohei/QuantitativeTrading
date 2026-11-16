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
    manual_error = None
    if shown is not None:
        today = deps.now().date()
        view = views.signals_page(
            s,
            found.user.id,
            shown.id,
            deps.calendar(),
            deps.cfg,
            today,
            names=stock_names(deps),
        )
        manual_error = views.manual_notice(
            s,
            found.user.id,
            shown.id,
            deps.market(today, views.book_symbols(s, found.user.id, shown.id)),
            deps.calendar(),
            deps.cfg,
            today,
        )
    context = {**context, "view": view, "manual_error": manual_error}
    return render(request, "signals.html", context, found=found)
