"""审计记录区分渠道与操作人：每个写函数分别以命令行、网页身份调用，核对审计行。"""

from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.db.models import AuditLog
from qt.features import build_panel
from qt.models import Side
from qt.service.accounts import (
    close_subscription,
    create_subscription,
    create_user,
    pause_subscription,
    resume_subscription,
    set_weekly_mail,
    update_params,
)
from qt.service.actor import CLI, JOB, Actor, web_actor
from qt.service.manual_ops import record_cash, record_holding, record_trade, void_entry
from qt.service.market import StoreMarket
from tests.fakes import populate_trading_store

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
S = 260  # 订阅起始日的交易日序号


def _last_audit(db: Session) -> AuditLog:
    return db.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first() or pytest.fail(
        "没有审计行"
    )


def test_actor_constants_and_web_actor() -> None:
    assert (CLI.user_id, CLI.channel) == (None, "cli")
    assert (JOB.user_id, JOB.channel) == (None, "job")
    assert web_actor(7) == Actor(7, "web")


@pytest.mark.parametrize("channel", ["cli", "web"])
def test_every_write_function_records_channel_and_actor(
    db: Session, tmp_path: Path, channel: str
) -> None:
    store = ParquetStore(tmp_path)
    days = populate_trading_store(store)
    calendar = TradingCalendar(days)
    market = StoreMarket(store, build_panel(store, calendar, CFG, days[0], days[-1]), CFG)
    admin = create_user(db, "admin@example.com", actor=CLI)
    # 网页渠道的操作人：创建使用者时是管理员，其余操作是使用者本人

    def check(uid: int, expected: Actor) -> None:
        row = _last_audit(db)
        assert row.channel == expected.channel
        assert row.actor_user_id == expected.user_id
        assert row.detail["user_id"] == uid

    creator = CLI if channel == "cli" else web_actor(admin.id)
    user = create_user(db, "u@example.com", actor=creator)
    check(user.id, creator)
    uid = user.id
    actor = CLI if channel == "cli" else web_actor(uid)

    sub_id: list[int] = []

    def subscribe() -> None:
        sub_id.append(create_subscription(db, uid, CFG, 100_000.0, days[S], actor=actor).id)

    today = days[S + 4]
    steps: list[Callable[[], object]] = [
        lambda: set_weekly_mail(db, uid, False, actor=actor),
        subscribe,
        lambda: update_params(
            db, uid, sub_id[0], CFG, {"strategy.take_profit_fixed": 0.25}, days[S], actor=actor
        ),
        lambda: record_trade(
            db,
            uid,
            sub_id[0],
            market,
            calendar,
            CFG,
            today,
            Side.BUY,
            "600003",
            days[S + 1],
            10.0,
            1000,
            actor=actor,
        ),
        lambda: record_holding(
            db,
            uid,
            sub_id[0],
            market,
            calendar,
            CFG,
            today,
            "600001",
            days[S],
            10.0,
            100,
            actor=actor,
        ),
        lambda: record_cash(
            db, uid, sub_id[0], market, calendar, CFG, today, "deposit", days[S], 500.0, actor=actor
        ),
    ]
    for step in steps:
        step()
        check(uid, actor)
    last_entry_id = _last_audit(db).target.removeprefix("ledger_entry:")
    void_entry(db, uid, sub_id[0], market, calendar, CFG, today, int(last_entry_id), actor=actor)
    check(uid, actor)
    for fn in (pause_subscription, resume_subscription, close_subscription):
        fn(db, uid, sub_id[0], actor=actor)
        check(uid, actor)
