import pytest
from sqlalchemy import Engine, text

from qt.db.engine import database_url, session_scope


def test_session_scope_commits_and_rolls_back(db_engine: Engine) -> None:
    with session_scope(db_engine) as s:
        s.execute(text("create table t_probe (id int primary key)"))
        s.execute(text("insert into t_probe values (1)"))
    with pytest.raises(RuntimeError), session_scope(db_engine) as s:
        s.execute(text("insert into t_probe values (2)"))
        raise RuntimeError("boom")
    with session_scope(db_engine) as s:
        assert s.execute(text("select count(*) from t_probe")).scalar_one() == 1
        s.execute(text("drop table t_probe"))


def test_database_url_missing_gives_chinese_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database_url()


def test_database_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/d")
    assert database_url() == "postgresql+psycopg://u:p@h/d"


def test_db_fixture_truncates_tables_between_tests(db_engine: Engine, db: object) -> None:
    del db
    with db_engine.begin() as conn:
        conn.execute(text("create table t_trunc (id int)"))
        conn.execute(text("insert into t_trunc values (1)"))


def test_db_fixture_cleaned_previous_rows(db_engine: Engine, db: object) -> None:
    del db
    with db_engine.begin() as conn:
        count = conn.execute(text("select count(*) from t_trunc")).scalar_one()
        conn.execute(text("drop table t_trunc"))
    assert count == 0
