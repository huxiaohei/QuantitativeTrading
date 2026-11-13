"""Alembic 环境：连接地址来自 Config.attributes["url"]、-x url=... 或环境变量 DATABASE_URL。"""

from alembic import context
from sqlalchemy import Connection

from qt.db.engine import database_url, make_engine

# 后续任务在此挂上 ORM 元数据；基线迁移没有表
target_metadata = None


def _url() -> str:
    given = context.config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get(
        "url"
    )
    return str(given) if given else database_url()


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    engine = make_engine(_url())
    try:
        with engine.connect() as connection:
            _migrate(connection)
    finally:
        engine.dispose()


run()
