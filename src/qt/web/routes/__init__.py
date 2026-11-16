"""各页面的路由。导入本包即填好 qt.web.app.ROUTES。"""

from fastapi import APIRouter

from qt.web.routes import overview, public

ROUTERS: tuple[APIRouter, ...] = (public.router, overview.router)
