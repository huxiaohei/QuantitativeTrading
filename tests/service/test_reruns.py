"""立即重试请求：有没有未完成的日子、登记去重、取走与写回结果。"""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.db.models import AuditLog, DailyRun
from qt.service import reruns
from qt.service.accounts import create_subscription, create_user
from qt.service.actor import CLI

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
SH = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 11, 16)
CALENDAR = TradingCalendar([date(2026, 11, 13), MONDAY])
RUN_TIME = CFG.service.run_time
EVENING = datetime(2026, 11, 16, 19, 0, tzinfo=SH)


def _admin_and_subscription(db: Session) -> int:
    admin = create_user(db, "admin@example.com", is_admin=True, actor=CLI)
    user = create_user(db, "a@example.com", actor=CLI)
    create_subscription(db, user.id, CFG, 100_000.0, MONDAY, actor=CLI)
    db.commit()
    return admin.id


def _run(db: Session, status: str) -> None:
    db.add(DailyRun(trade_day=MONDAY, status=status, stage="x", started_at=EVENING, detail={}))
    db.commit()


def test_no_unfinished_day_when_everything_done(db: Session) -> None:
    _admin_and_subscription(db)
    _run(db, "done")
    assert not reruns.has_unfinished_day(db, CALENDAR, EVENING, RUN_TIME)


def test_unfinished_day_after_data_failed(db: Session) -> None:
    _admin_and_subscription(db)
    _run(db, "data_failed")
    assert reruns.has_unfinished_day(db, CALENDAR, EVENING, RUN_TIME)


def test_not_unfinished_before_run_time_or_without_subscription(db: Session) -> None:
    morning = datetime(2026, 11, 16, 9, 0, tzinfo=SH)
    assert not reruns.has_unfinished_day(db, CALENDAR, EVENING, RUN_TIME)  # 没有订阅
    _admin_and_subscription(db)
    assert not reruns.has_unfinished_day(db, CALENDAR, morning, RUN_TIME)  # 还没到运行时间


def test_request_is_deduplicated_and_audited(db: Session) -> None:
    admin_id = _admin_and_subscription(db)
    first = reruns.request(db, admin_id, EVENING)
    again = reruns.request(db, admin_id, EVENING)
    assert again.id == first.id
    db.commit()
    audits = list(db.scalars(select(AuditLog).where(AuditLog.action == "rerun_request")))
    assert len(audits) == 1
    assert audits[0].channel == "web" and audits[0].actor_user_id == admin_id


def test_take_pending_and_finish_flow(db: Session) -> None:
    admin_id = _admin_and_subscription(db)
    assert reruns.take_pending(db) is None
    assert reruns.latest(db) is None
    row = reruns.request(db, admin_id, EVENING)
    db.commit()
    taken = reruns.take_pending(db)
    assert taken is not None and taken.id == row.id
    assert taken.handled_at is None
    reruns.finish(db, row.id, "2026-11-16 done", EVENING)
    db.commit()
    assert reruns.take_pending(db) is None
    latest = reruns.latest(db)
    assert latest is not None
    assert latest.result == "2026-11-16 done" and latest.handled_at == EVENING
    # 处理完之后再请求会新建一条
    assert reruns.request(db, admin_id, EVENING).id != row.id
