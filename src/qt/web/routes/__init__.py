"""各页面的路由。导入本包即填好 qt.web.app.ROUTES。"""

from fastapi import APIRouter

from qt.web.routes import (
    admin,
    journal,
    manual,
    overview,
    public,
    settings,
    signals,
    strategy,
    system,
)

ROUTERS: tuple[APIRouter, ...] = (
    public.router,
    overview.router,
    signals.router,
    system.router,
    journal.router,
    manual.router,
    strategy.router,
    settings.router,
    admin.router,
)
