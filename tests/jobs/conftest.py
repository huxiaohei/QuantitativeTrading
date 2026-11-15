"""每日任务与定时进程的测试夹具：临时数据目录、假数据源、收集邮件的发送器、可控时钟。"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import DailyRun, Signal, Subscription
from qt.jobs.daily import RunContext
from qt.service.accounts import create_subscription, create_user
from tests.fakes import FakeSource, populate_trading_store

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
SH = ZoneInfo("Asia/Shanghai")
ADMIN = "admin@example.com"
CASH = CFG.backtest.initial_cash  # 与 run_period 的站点本金相同，便于与回测比较
DAY = 260  # 2022-01-03（周一）：四只股票都满足买入条件


@dataclass(frozen=True)
class SentMail:
    to_addr: str
    subject: str
    html: str
    text: str


class CollectingSender:
    """把发出的邮件收集到列表；failing 为真时全部失败，fail_to 里的地址失败。"""

    def __init__(self) -> None:
        self.sent: list[SentMail] = []
        self.failing = False
        self.fail_to: set[str] = set()

    def send(self, to_addr: str, subject: str, html: str, text: str) -> None:
        if self.failing or to_addr in self.fail_to:
            raise ConnectionError("模拟 SMTP 不可用")
        self.sent.append(SentMail(to_addr, subject, html, text))


@dataclass
class FakeClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class BrokenSource(FakeSource):
    """证券列表取不到：update_all 抛 SharedTableError。"""

    def securities(self) -> pd.DataFrame:
        self.calls.append(("securities", ()))
        raise ConnectionError("模拟数据源不可用")


def mirror_source(store: ParquetStore, cls: type[FakeSource] = FakeSource) -> FakeSource:
    """数据源内容与存储完全相同（更新时不会带来任何变化）。"""
    return cls(
        trade_days=store.read(Table.TRADE_DAYS),
        securities=store.read(Table.SECURITIES),
        bars=store.read(Table.BARS),
        dividends=store.read(Table.DIVIDENDS),
        financials=store.read(Table.FINANCIALS),
        industries=store.read(Table.INDUSTRIES),
    )


def evening(day: date, hour: int = 20, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SH)


@dataclass
class Env:
    engine: Engine
    store: ParquetStore
    days: list[date]
    clock: FakeClock
    sender: CollectingSender = field(default_factory=CollectingSender)
    source: DataSource | None = None

    @property
    def calendar(self) -> TradingCalendar:
        return TradingCalendar(self.days)

    @property
    def ctx(self) -> RunContext:
        source = self.source if self.source is not None else mirror_source(self.store)
        self.source = source
        return RunContext(
            engine=self.engine,
            store=self.store,
            source=source,
            cfg=CFG,
            sender=self.sender,
            admin_email=ADMIN,
            now=self.clock,
        )


@pytest.fixture
def env(tmp_path: Path, db_engine: Engine, db: Session) -> Iterator[Env]:
    store = ParquetStore(tmp_path / "data")
    days = populate_trading_store(store)
    yield Env(db_engine, store, days, FakeClock(evening(days[DAY])))


def subscribe(
    db: Session,
    email: str,
    start: date,
    overrides: dict[str, object] | None = None,
) -> Subscription:
    user = create_user(db, email)
    sub = create_subscription(db, user.id, CFG, CASH, start, overrides)
    db.commit()
    return sub


def processed_through(db: Session, sub: Subscription, kind: str) -> date | None:
    db.rollback()
    return repo.get_book_of_kind(db, sub.user_id, sub.id, kind).processed_through


def book_signals(db: Session, sub: Subscription, kind: str) -> list[Signal]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, kind)
    return list(db.scalars(select(Signal).where(Signal.book_id == book.id).order_by(Signal.id)))


def daily_run(db: Session, day: date) -> DailyRun | None:
    db.rollback()
    return db.get(DailyRun, day)
