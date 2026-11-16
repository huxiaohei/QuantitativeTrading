"""数据库引擎与事务边界。"""

import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    """读取环境变量 DATABASE_URL；缺失或为空时抛出中文错误。"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("未设置环境变量 DATABASE_URL，请在 .env 中配置数据库连接地址")
    return url


def make_engine(url: str) -> Engine:
    """创建引擎；连接前先探测，避免使用已被服务端断开的连接。

    hide_parameters：报错文字里不带 SQL 参数值（邮件正文里的链接令牌、防伪造令牌等），
    免得经异常信息进日志。
    """
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@contextmanager
def session_scope(engine: Engine) -> Generator[Session]:
    """事务上下文：正常退出提交，抛出异常则回滚并继续抛出。"""
    session = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
