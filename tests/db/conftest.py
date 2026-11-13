"""数据库测试夹具：对测试库执行迁移；每个测试结束后清空所有表。

数据库连不上时测试直接报错，不跳过。其他测试目录需要时在各自 conftest 中
`pytest_plugins = ["tests.db.conftest"]` 复用。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from qt.db.engine import make_engine

_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = "postgresql+psycopg://qt:qt@127.0.0.1:54329/qt_test"
# 迁移版本记录表不清空
_KEEP_TABLES = {"alembic_version"}


def resolve_test_db_url() -> str:
    """测试库地址；可用 QT_TEST_DATABASE_URL 覆盖。"""
    return os.environ.get("QT_TEST_DATABASE_URL", TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = resolve_test_db_url()
    engine = make_engine(url)
    try:
        # 重建干净的 schema，再升级到最新
        with engine.begin() as conn:
            conn.execute(text("drop schema public cascade"))
            conn.execute(text("create schema public"))
        config = Config(str(_ROOT / "alembic.ini"))
        config.attributes["url"] = url
        command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()


def truncate_all(engine: Engine) -> None:
    """清空 public 下除迁移版本表外的所有表。"""
    with engine.begin() as conn:
        names = conn.execute(
            text("select tablename from pg_tables where schemaname = 'public'")
        ).scalars()
        tables = [n for n in names if n not in _KEEP_TABLES]
        if tables:
            joined = ", ".join(f'"{n}"' for n in tables)
            conn.execute(text(f"truncate table {joined} restart identity cascade"))


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    session = Session(db_engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        truncate_all(db_engine)
