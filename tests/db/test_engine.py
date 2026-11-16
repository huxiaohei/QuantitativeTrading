import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qt.db.engine import database_url, session_scope
from tests.conftest import truncate_all


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


def test_truncate_all_clears_rows_but_keeps_tables(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("create table t_trunc (id int)"))
        conn.execute(text("insert into t_trunc values (1)"))
    try:
        truncate_all(db_engine)
        with db_engine.begin() as conn:
            assert conn.execute(text("select count(*) from t_trunc")).scalar_one() == 0
            version = conn.execute(text("select count(*) from alembic_version")).scalar_one()
            assert version == 1
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("drop table t_trunc"))


def test_db_fixture_gives_working_session(db: Session) -> None:
    assert db.execute(text("select 1")).scalar_one() == 1


def test_errors_do_not_carry_parameter_values(db_engine: Engine) -> None:
    """报错文字里不带 SQL 参数值（邮件正文里的令牌、防伪造令牌等不会经异常进日志）。

    冲突的是主键 k，秘密只在另一列 v 的参数里；数据库自己的 DETAIL 只报主键的值。
    """
    secret = "secret-token-value-XYZ"
    with db_engine.begin() as conn:
        conn.execute(text("create table t_secret (k int primary key, v text)"))
    try:
        with pytest.raises(IntegrityError) as exc, db_engine.begin() as conn:
            for _ in range(2):
                conn.execute(text("insert into t_secret values (1, :v)"), {"v": secret})
        assert "t_secret" in str(exc.value)
        assert secret not in str(exc.value)
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("drop table t_secret"))
