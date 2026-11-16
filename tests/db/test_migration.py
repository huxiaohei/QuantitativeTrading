"""迁移与模型一致：升级到最新后，库结构与 ORM 元数据没有差异；0003 可升可降。"""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from qt.db.models import Base
from tests.conftest import resolve_test_db_url

_ROOT = Path(__file__).resolve().parents[2]
_NEW_TABLES = {"invites", "web_sessions", "email_tokens", "rate_events", "rerun_requests"}


def test_models_match_migrated_schema(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def _web_schema(engine: Engine) -> tuple[set[str], set[str], set[str]]:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    user_cols = {c["name"] for c in insp.get_columns("users")}
    audit_cols = {c["name"] for c in insp.get_columns("audit_log")}
    return tables, user_cols, audit_cols


def test_0003_upgrade_adds_and_downgrade_removes_web_schema(db_engine: Engine) -> None:
    tables, user_cols, audit_cols = _web_schema(db_engine)
    assert tables >= _NEW_TABLES
    assert "abandon_mail" in user_cols
    assert "channel" in audit_cols

    config = Config(str(_ROOT / "alembic.ini"))
    config.attributes["url"] = resolve_test_db_url()
    command.downgrade(config, "0002")
    try:
        tables, user_cols, audit_cols = _web_schema(db_engine)
        assert not _NEW_TABLES & tables
        assert "abandon_mail" not in user_cols
        assert "channel" not in audit_cols
    finally:
        command.upgrade(config, "head")
    assert _web_schema(db_engine)[0] >= _NEW_TABLES
