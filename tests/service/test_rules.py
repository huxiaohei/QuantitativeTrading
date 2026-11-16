"""命令行与网页共用的规则：生效日、起始日、订阅查看口径、参数说明、放弃提醒开关。"""

from datetime import date, timedelta
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, StrategyConfig, load_config
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import AuditLog
from qt.service import rules
from qt.service.accounts import (
    close_subscription,
    create_subscription,
    create_user,
    set_abandon_mail,
)
from qt.service.actor import CLI, web_actor
from qt.service.errors import ValidationError
from tests.fakes import populate_trading_store

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
S = 260
LIMIT = CFG.service.backdate_limit_days
ORIGINAL_KEYS = {
    "strategy.min_dividend_yield",
    "strategy.max_price_position",
    "strategy.lookback_days",
    "strategy.min_dividend_years",
    "strategy.exclude_dividend_cut",
    "strategy.min_roe_avg3",
    "strategy.max_payout_ratio",
    "strategy.min_profit_yoy",
    "strategy.max_debt_ratio",
    "strategy.take_profit_mode",
    "strategy.take_profit_fixed",
    "strategy.trailing_activate",
    "strategy.trailing_drawdown",
    "strategy.stop_loss",
    "strategy.time_stop_days",
    "strategy.fundamental_profit_yoy",
    "risk.max_position_pct",
    "risk.max_positions",
    "risk.max_industry_pct",
    "risk.warn_drawdown",
    "risk.pause_drawdown",
    "risk.pause_days",
    "risk.cooldown_days",
}


@pytest.fixture
def days(tmp_path: Path) -> list[date]:
    return populate_trading_store(ParquetStore(tmp_path))


def test_params_cover_every_original_key_with_clean_descriptions() -> None:
    assert {p.key for p in rules.PARAMS} == ORIGINAL_KEYS
    assert len(rules.PARAMS) == len(ORIGINAL_KEYS)
    for p in rules.PARAMS:
        assert p.label and p.description, p.key
        assert "建议" not in p.description and "推荐" not in p.description, p.key


def test_param_kinds_and_choices_are_truthful() -> None:
    by_key = {p.key: p for p in rules.PARAMS}
    assert by_key["strategy.exclude_dividend_cut"].kind == "bool"
    assert by_key["strategy.take_profit_mode"].kind == "choice"
    allowed = get_args(StrategyConfig.model_fields["take_profit_mode"].annotation)
    for p in rules.PARAMS:
        if p.kind == "choice":
            assert p.choices and all(label for _, label in p.choices), p.key
            assert {value for value, _ in p.choices} == set(allowed), p.key
        else:
            assert p.choices == (), p.key


def test_next_effective_day_follows_the_earlier_book(db: Session, days: list[date]) -> None:
    calendar = TradingCalendar(days)
    user = create_user(db, "r@example.com", actor=CLI)
    sub = create_subscription(db, user.id, CFG, 100_000.0, days[S], actor=CLI)
    assert rules.next_effective_day(db, sub, calendar) == days[S]  # 还没有运行过
    system = repo.get_book_of_kind(db, user.id, sub.id, "system")
    system.processed_through = days[S + 1]
    assert rules.next_effective_day(db, sub, calendar) == days[S]  # 手动账还没推进
    manual = repo.get_book_of_kind(db, user.id, sub.id, "manual")
    manual.processed_through = days[S + 2]
    assert rules.next_effective_day(db, sub, calendar) == days[S + 2]
    system.processed_through = days[-1]
    manual.processed_through = days[-1]
    with pytest.raises(ValidationError, match="交易日历"):
        rules.next_effective_day(db, sub, calendar)


def test_resolve_start_day_accepts_default_and_valid_choices(days: list[date]) -> None:
    calendar = TradingCalendar(days)
    today = days[262]
    default = days[263]
    assert rules.resolve_start_day(None, calendar, default, LIMIT, today=today) == default
    for ok in (days[262 - LIMIT], default):
        assert rules.resolve_start_day(str(ok), calendar, default, LIMIT, today=today) == ok


def test_resolve_start_day_rejects_bad_input_in_chinese(days: list[date]) -> None:
    calendar = TradingCalendar(days)
    today = days[262]
    default = days[263]
    cases = {
        str(days[262] + timedelta(days=3)): "不是交易日",
        str(days[262 - LIMIT - 1]): "太早",
        str(days[264]): "不能晚于",
        "2022-13-40": "日期",
    }
    for text, message in cases.items():
        with pytest.raises(ValidationError, match=message):
            rules.resolve_start_day(text, calendar, default, LIMIT, today=today)


def test_shown_subscription_prefers_open_then_latest(db: Session, days: list[date]) -> None:
    user = create_user(db, "s@example.com", actor=CLI)
    assert rules.shown_subscription(db, user.id) is None
    assert rules.list_subscriptions(db, user.id) == []
    first = create_subscription(db, user.id, CFG, 1e5, days[S], actor=CLI)
    assert rules.shown_subscription(db, user.id) == first
    close_subscription(db, user.id, first.id, actor=CLI)
    assert rules.shown_subscription(db, user.id) == first  # 只有已关闭的：取最近一个
    second = create_subscription(db, user.id, CFG, 1e5, days[S], actor=CLI)
    assert rules.shown_subscription(db, user.id) == second
    assert rules.list_subscriptions(db, user.id) == [second, first]


def test_set_abandon_mail_switches_flag_and_audits(db: Session) -> None:
    user = create_user(db, "m@example.com", actor=CLI)
    assert user.abandon_mail is True
    set_abandon_mail(db, user.id, False, actor=web_actor(user.id))
    assert repo.get_user(db, user.id).abandon_mail is False
    row = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert row is not None
    assert row.action == "user.abandon_mail"
    assert row.channel == "web" and row.actor_user_id == user.id
    assert row.detail == {"enabled": False, "user_id": user.id}
