"""只读页面共用的部分：按网址里的 sub 取订阅（归属由服务层校验）、页面顶部需要的数据。"""

from typing import Annotated

from fastapi import Query
from sqlalchemy.orm import Session

from qt.db.models import Subscription
from qt.service import views
from qt.service.market import security_names
from qt.web.deps import Current, WebDeps

# 网址里可选的订阅编号；不是本人的由 views.resolve_subscription 抛 NotFoundError（应用转成 404）
SubParam = Annotated[int | None, Query()]


def page_context(
    s: Session, found: Current, sub_id: int | None, active: str, path: str
) -> tuple[Subscription | None, dict[str, object]]:
    """要看的订阅与每个只读页面顶部共用的模板数据（订阅、最近一次运行、可切换的订阅）。"""
    sub = views.resolve_subscription(s, found.user.id, sub_id)
    context: dict[str, object] = {
        "active": active,
        "path": path,
        "run": views.latest_run(s),
        "subscription": None if sub is None else views.subscription_view(sub),
        "subscriptions": views.subscriptions(s, found.user.id),
    }
    return sub, context


def stock_names(deps: WebDeps) -> dict[str, str]:
    """股票代码 -> 名称。"""
    return security_names(deps.store)
