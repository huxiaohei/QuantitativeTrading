"""使用者与订阅：创建、参数校验、参数历史、配置合并、审计。"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.config import AppConfig, load_config
from qt.db import repo
from qt.db.models import AuditLog
from qt.service.accounts import (
    close_subscription,
    create_subscription,
    create_user,
    pause_subscription,
    resume_subscription,
    set_weekly_mail,
    subscription_config,
    update_params,
)
from qt.service.actor import CLI
from qt.service.errors import ValidationError

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
DAY = date(2026, 11, 16)


def _user_and_sub(db: Session) -> tuple[int, int]:
    u = create_user(db, "a@example.com", actor=CLI)
    sub = create_subscription(db, u.id, CFG, 150_000.0, DAY, actor=CLI)
    return u.id, sub.id


def test_create_subscription_creates_two_books_with_default_params(db: Session) -> None:
    u = create_user(db, "A@Example.com", actor=CLI)
    assert u.email == "a@example.com"
    sub = create_subscription(db, u.id, CFG, 150_000.0, DAY, actor=CLI)
    assert sub.params["strategy"]["take_profit_fixed"] == 0.20
    assert sub.params["risk"]["max_positions"] == 10
    assert {b.kind for b in repo.list_books(db, u.id, sub.id)} == {"system", "manual"}
    assert sub.status == "active"
    assert sub.initial_cash == 150_000.0


def test_duplicate_email_rejected(db: Session) -> None:
    create_user(db, "a@example.com", actor=CLI)
    with pytest.raises(ValidationError, match="已注册"):
        create_user(db, "A@example.com", actor=CLI)


def test_invalid_email_rejected(db: Session) -> None:
    with pytest.raises(ValidationError, match="邮箱"):
        create_user(db, "not-an-email", actor=CLI)


@pytest.mark.parametrize(
    "bad",
    [
        "a@b@example.com",
        "@example.com",
        "a@",
        "a@localhost",
        "a@example.",
        "a@.com",
        "a@example..com",
        "a b@example.com",
        "a@exa mple.com",
        "a\x00@example.com",
        "a\x07b@example.com",
        "a@exam\x1bple.com",
        "a@example.com\nb",
        "a\u200b@example.com",
        "a" * 250 + "@example.com",
    ],
)
def test_malformed_email_rejected_with_chinese_message(db: Session, bad: str) -> None:
    with pytest.raises(ValidationError, match="邮箱格式"):
        create_user(db, bad, actor=CLI)


def test_email_of_maximum_length_is_accepted(db: Session) -> None:
    address = "a" * (254 - len("@example.com")) + "@example.com"
    assert len(address) == 254
    assert create_user(db, address, actor=CLI).email == address
    with pytest.raises(ValidationError, match="邮箱格式"):
        create_user(db, "a" + address, actor=CLI)


def test_set_weekly_mail_switches_flag_and_audits(db: Session) -> None:
    user = create_user(db, "w@example.com", actor=CLI)
    assert user.weekly_mail is True
    set_weekly_mail(db, user.id, False, actor=CLI)
    assert repo.get_user(db, user.id).weekly_mail is False
    set_weekly_mail(db, user.id, True, actor=CLI)
    assert repo.get_user(db, user.id).weekly_mail is True
    actions = list(db.scalars(select(AuditLog.action).where(AuditLog.target == f"user:{user.id}")))
    assert actions.count("user.weekly_mail") == 2


def test_only_one_active_subscription_per_user(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    with pytest.raises(ValidationError, match="已有生效的订阅"):
        create_subscription(db, uid, CFG, 1e5, DAY, actor=CLI)
    close_subscription(db, uid, sid, actor=CLI)
    again = create_subscription(db, uid, CFG, 1e5, DAY, actor=CLI)
    assert again.id != sid


def test_paused_subscription_still_counts_as_active_slot(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    pause_subscription(db, uid, sid, actor=CLI)
    with pytest.raises(ValidationError, match="已有生效的订阅"):
        create_subscription(db, uid, CFG, 1e5, DAY, actor=CLI)


def test_lookback_days_and_site_sections_cannot_be_overridden(db: Session) -> None:
    uid = create_user(db, "a@example.com", actor=CLI).id
    for path in ("strategy.lookback_days", "universe.min_listed_years", "costs.slippage"):
        with pytest.raises(ValidationError, match="全站统一"):
            create_subscription(db, uid, CFG, 1e5, DAY, {path: 1}, actor=CLI)


def test_invalid_param_value_rejected_with_same_rules_as_config(db: Session) -> None:
    uid = create_user(db, "a@example.com", actor=CLI).id
    with pytest.raises(ValidationError):
        create_subscription(db, uid, CFG, 1e5, DAY, {"strategy.stop_loss": 1.5}, actor=CLI)
    with pytest.raises(ValidationError):
        create_subscription(db, uid, CFG, 1e5, DAY, {"strategy.no_such": 1}, actor=CLI)


def test_initial_cash_must_be_positive(db: Session) -> None:
    uid = create_user(db, "a@example.com", actor=CLI).id
    with pytest.raises(ValidationError, match="本金"):
        create_subscription(db, uid, CFG, 0.0, DAY, actor=CLI)


def test_overrides_at_creation_are_stored(db: Session) -> None:
    uid = create_user(db, "a@example.com", actor=CLI).id
    sub = create_subscription(db, uid, CFG, 1e5, DAY, {"risk.max_positions": 5}, actor=CLI)
    assert sub.params["risk"]["max_positions"] == 5


def test_update_params_records_history_and_keeps_books(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    update_params(db, uid, sid, CFG, {"strategy.stop_loss": 0.10}, date(2026, 11, 17), actor=CLI)
    (change,) = repo.list_param_changes(db, uid, sid)
    assert change.old["strategy"]["stop_loss"] == 0.15
    assert change.new["strategy"]["stop_loss"] == 0.10
    assert change.effective_day == date(2026, 11, 17)
    assert len(repo.list_books(db, uid, sid)) == 2


def test_update_params_rejects_closed_subscription_and_bad_values(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    with pytest.raises(ValidationError, match="全站统一"):
        update_params(db, uid, sid, CFG, {"strategy.lookback_days": 100}, DAY, actor=CLI)
    close_subscription(db, uid, sid, actor=CLI)
    with pytest.raises(ValidationError, match="已关闭"):
        update_params(db, uid, sid, CFG, {"strategy.stop_loss": 0.1}, DAY, actor=CLI)


def test_subscription_config_merges_user_params_over_site_config(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    sub = update_params(db, uid, sid, CFG, {"strategy.stop_loss": 0.10}, DAY, actor=CLI)
    merged = subscription_config(CFG, sub)
    assert merged.strategy.stop_loss == 0.10
    assert merged.costs == CFG.costs and merged.universe == CFG.universe
    assert merged.service == CFG.service


def test_pause_resume_close_transitions(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    pause_subscription(db, uid, sid, actor=CLI)
    assert repo.get_subscription(db, uid, sid).status == "paused"
    resume_subscription(db, uid, sid, actor=CLI)
    assert repo.get_subscription(db, uid, sid).status == "active"
    close_subscription(db, uid, sid, actor=CLI)
    sub = repo.get_subscription(db, uid, sid)
    assert sub.status == "closed" and sub.closed_at is not None
    assert sub.closed_at.tzinfo is not None
    with pytest.raises(ValidationError):
        resume_subscription(db, uid, sid, actor=CLI)
    with pytest.raises(ValidationError):
        pause_subscription(db, uid, sid, actor=CLI)


def test_mutations_write_audit_log(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    update_params(db, uid, sid, CFG, {"strategy.stop_loss": 0.10}, DAY, actor=CLI)
    pause_subscription(db, uid, sid, actor=CLI)
    resume_subscription(db, uid, sid, actor=CLI)
    close_subscription(db, uid, sid, actor=CLI)
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions == [
        "user.create",
        "subscription.create",
        "subscription.update_params",
        "subscription.pause",
        "subscription.resume",
        "subscription.close",
    ]


def test_initial_cash_must_be_finite(db: Session) -> None:
    uid = create_user(db, "a@example.com", actor=CLI).id
    for bad in (float("inf"), float("nan"), -1.0):
        with pytest.raises(ValidationError, match="本金"):
            create_subscription(db, uid, CFG, bad, DAY, actor=CLI)


def test_update_params_without_change_rejected_and_writes_nothing(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    with pytest.raises(ValidationError, match="参数没有变化"):
        update_params(db, uid, sid, CFG, {}, DAY, actor=CLI)
    with pytest.raises(ValidationError, match="参数没有变化"):
        update_params(db, uid, sid, CFG, {"strategy.stop_loss": 0.15}, DAY, actor=CLI)
    assert repo.list_param_changes(db, uid, sid) == []
    actions = [a.action for a in db.scalars(select(AuditLog))]
    assert "subscription.update_params" not in actions


def test_update_params_audit_stores_normalized_values(db: Session) -> None:
    uid, sid = _user_and_sub(db)
    update_params(db, uid, sid, CFG, {"strategy.stop_loss": "0.10"}, DAY, actor=CLI)
    entry = db.scalars(
        select(AuditLog).where(AuditLog.action == "subscription.update_params")
    ).one()
    assert entry.detail["overrides"] == {"strategy.stop_loss": 0.10}
