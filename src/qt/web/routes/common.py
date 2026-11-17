"""只读页面共用的部分：按网址里的 sub 取订阅（归属由服务层校验）、页面顶部需要的数据。"""

from collections.abc import Sequence
from typing import Annotated

from fastapi import Query
from sqlalchemy.orm import Session

from qt.data.quotes import in_session
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


LIVE_SECONDS = 15


def live_context(
    deps: WebDeps, rows: Sequence[views.HoldingRow], url: str, *, levels: bool
) -> dict[str, object]:
    """持仓片段的模板数据。交易时段内取盘中报价并让页面每 LIVE_SECONDS 秒重新取这一块；

    其余时间不请求报价，显示收盘价，也不带刷新地址（页面随之停止刷新）。
    """
    now = deps.now()
    polling = bool(rows) and in_session(now, deps.calendar())
    quotes = deps.quotes([row.symbol for row in rows], now) if polling else {}
    return {
        "live": views.apply_quotes(rows, quotes),
        "live_url": url if polling else None,
        "live_seconds": LIVE_SECONDS,
        "levels": levels,
    }
