"""迁移与模型一致：升级到最新后，库结构与 ORM 元数据没有差异。"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from qt.db.models import Base


def test_models_match_migrated_schema(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
