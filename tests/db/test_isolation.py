"""多用户隔离：业务查询函数拒绝他人数据，且第一、二个参数固定为 s、user_id。"""

import inspect
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from qt.config import AppConfig, load_config
from qt.db import admin_repo, repo
from qt.service.accounts import (
    close_subscription,
    create_subscription,
    create_user,
    pause_subscription,
    resume_subscription,
    update_params,
)
from qt.service.errors import NotFoundError

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
DAY = date(2026, 11, 16)


def test_repo_functions_refuse_other_users_rows(db: Session) -> None:
    user_a = create_user(db, "a@example.com")
    user_b = create_user(db, "b@example.com")
    sub_a = create_subscription(db, user_a.id, CFG, 1e5, DAY)
    create_subscription(db, user_b.id, CFG, 1e5, DAY)
    with pytest.raises(NotFoundError):
        repo.get_subscription(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        repo.list_books(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        repo.list_param_changes(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        update_params(db, user_b.id, sub_a.id, CFG, {"strategy.stop_loss": 0.1}, DAY)
    with pytest.raises(NotFoundError):
        pause_subscription(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        resume_subscription(db, user_b.id, sub_a.id)
    with pytest.raises(NotFoundError):
        close_subscription(db, user_b.id, sub_a.id)
    # 数据没有被动过
    assert repo.get_subscription(db, user_a.id, sub_a.id).status == "active"


def test_get_book_refuses_other_users_book(db: Session) -> None:
    user_a = create_user(db, "a@example.com")
    user_b = create_user(db, "b@example.com")
    sub_a = create_subscription(db, user_a.id, CFG, 1e5, DAY)
    book = repo.list_books(db, user_a.id, sub_a.id)[0]
    assert repo.get_book(db, user_a.id, book.id).id == book.id
    with pytest.raises(NotFoundError):
        repo.get_book(db, user_b.id, book.id)
    with pytest.raises(NotFoundError):
        repo.get_user(db, 99999)


def test_admin_repo_lists_across_users(db: Session) -> None:
    user_a = create_user(db, "a@example.com")
    user_b = create_user(db, "b@example.com")
    create_subscription(db, user_a.id, CFG, 1e5, DAY)
    sub_b = create_subscription(db, user_b.id, CFG, 1e5, DAY)
    pause_subscription(db, user_b.id, sub_b.id)
    assert {s.user_id for s in admin_repo.list_active_subscriptions(db)} == {user_a.id}
    assert {u.email for u in admin_repo.list_users(db)} == {"a@example.com", "b@example.com"}
    found = admin_repo.find_user_by_email(db, "A@Example.com")
    assert found is not None and found.id == user_a.id
    assert admin_repo.find_user_by_email(db, "nobody@example.com") is None


def test_every_repo_function_takes_user_id_second() -> None:
    checked = 0
    for name, fn in inspect.getmembers(repo, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != repo.__name__:
            continue
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["s", "user_id"], name
        checked += 1
    assert checked > 0
