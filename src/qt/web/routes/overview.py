"""总览页（占位）：登录后的落脚页。完整的总览由后续任务替换。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from qt.web.app import route
from qt.web.deps import CurrentUser, render

router = APIRouter()


@route(router, "GET", "/overview", "user")
def overview(request: Request, found: CurrentUser) -> Response:
    return render(request, "base.html", {"placeholder": "总览页面建设中。"}, found=found)
