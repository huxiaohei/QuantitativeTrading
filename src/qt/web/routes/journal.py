"""日志页：两本账已完成的交易，以及离场、放弃、被否决之后的跟踪记录。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from qt.service import views
from qt.web.app import route
from qt.web.deps import CurrentUser, DbSession, Deps, render
from qt.web.routes.common import SubParam, page_context, stock_names

router = APIRouter()


@route(router, "GET", "/journal", "user")
def journal(
    request: Request, found: CurrentUser, deps: Deps, s: DbSession, sub: SubParam = None
) -> Response:
    shown, context = page_context(s, found, sub, "journal", "/journal")
    view = None
    if shown is not None:
        view = views.journal(s, found.user.id, shown.id, names=stock_names(deps))
    return render(request, "journal.html", {**context, "view": view}, found=found)
