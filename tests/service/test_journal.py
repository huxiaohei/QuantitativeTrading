"""交易日志的跟踪记录：事件建档、幂等、到期填收益（复权价、停牌、退市）。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.db import admin_repo, repo
from qt.db.models import Book, Followup, Signal, Subscription, Trade
from qt.db.models import Rejection as RejectionRow
from qt.features import build_panel
from qt.service.accounts import create_subscription, create_user
from qt.service.actor import CLI
from qt.service.journal import fill_followups, open_followups
from qt.service.market import StoreMarket
from tests.fakes import (
    make_bars,
    make_industries,
    make_securities,
    populate_store,
    weekdays,
    write_calendar,
)

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
N = 100
EVENT = 10  # 事件日的交易日序号
EX = 50  # 除权日序号：未复权收盘价减半、复权因子翻倍
SYMBOLS = ("600002", "600003", "600004", "600005")


def _adj_prices(symbol: str) -> list[float]:
    """复权收盘价：事件日 10.0，第 20 个交易日 11.0，第 60 个交易日 9.0，其余 10.0。"""
    prices = [10.0] * N
    if symbol == "600003":  # 到期日 30 停牌，其后第一天 31 价格 12.0
        prices[31] = 12.0
    else:
        prices[EVENT + 20] = 11.0
        prices[EVENT + 60] = 9.0
    return prices


@dataclass(frozen=True)
class World:
    store: ParquetStore
    calendar: TradingCalendar
    market: StoreMarket
    days: list[date]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    store = ParquetStore(tmp_path_factory.mktemp("journal"))
    days = weekdays(date(2024, 1, 2), N)
    write_calendar(store, days)
    frames: list[pd.DataFrame] = []
    for symbol in SYMBOLS:
        adj = _adj_prices(symbol)
        factors = [1.0 if i < EX else 2.0 for i in range(N)]
        closes = [a / f for a, f in zip(adj, factors, strict=True)]
        frame: pd.DataFrame = make_bars(symbol, days, closes, factors)
        if symbol == "600003":
            frame = frame.drop(index=EVENT + 20)  # 到期日停牌
        if symbol == "600004":
            frame = frame.iloc[: EVENT + 11]  # 事件日之后 10 天退市
        if symbol == "600005":
            frame = frame.iloc[EVENT + 1 :]  # 事件日及之前没有行情
        frames.append(frame.reset_index(drop=True))
    populate_store(
        store,
        securities=make_securities([(s, "sh_main", date(2010, 1, 1), None) for s in SYMBOLS]),
        bars=frames,
        industries=make_industries([(s, "银行", False) for s in SYMBOLS]),
    )
    calendar = TradingCalendar(days)
    panel = build_panel(store, calendar, CFG, days[0], days[-1])
    return World(store, calendar, StoreMarket(store, panel, CFG), days)


def _book(db: Session, email: str = "j@example.com") -> Book:
    user = create_user(db, email, actor=CLI)
    sub: Subscription = create_subscription(
        db, user.id, CFG, 100_000.0, date(2024, 1, 2), actor=CLI
    )
    db.commit()
    return repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")


def _trade(db: Session, book: Book, symbol: str, exit_day: date) -> None:
    db.add(
        Trade(
            book_id=book.id,
            user_id=book.user_id,
            symbol=symbol,
            entry_day=date(2024, 1, 2),
            exit_day=exit_day,
            shares=100.0,
            cost=1000.0,
            proceeds=1100.0,
            cash_received=0.0,
            exit_reason="manual",
            holding_days=5,
            entry_metrics={},
            origin="self",
            fill_price=11.0,
        )
    )


def _signal(db: Session, book: Book, symbol: str, status: str, status_day: date) -> None:
    db.add(
        Signal(
            book_id=book.id,
            user_id=book.user_id,
            side="buy",
            symbol=symbol,
            signal_day=date(2024, 1, 2),
            shares=100.0,
            ref_price=10.0,
            reason="r",
            metrics={},
            status=status,
            status_day=status_day,
        )
    )


def _rejection(db: Session, book: Book, symbol: str, first: date, last: date) -> None:
    db.add(
        RejectionRow(
            book_id=book.id,
            user_id=book.user_id,
            symbol=symbol,
            rule="r",
            message="m",
            metrics={},
            first_day=first,
            last_day=last,
        )
    )


def _followups(db: Session) -> list[Followup]:
    return list(db.scalars(select(Followup).order_by(Followup.id)))


def _seed_events(db: Session, w: World, book: Book) -> None:
    d = w.days
    _trade(db, book, "600002", d[EVENT])
    _trade(db, book, "600003", d[EVENT - 1])  # 不是当日离场
    _signal(db, book, "600003", "abandoned", d[EVENT])
    _signal(db, book, "600004", "executed", d[EVENT])  # 不是放弃
    _rejection(db, book, "600004", d[EVENT], d[EVENT + 3])
    _rejection(db, book, "600002", d[EVENT - 4], d[EVENT])  # 首日不是当日（重复否决）
    db.commit()


def test_followup_created_for_exit_abandoned_and_first_rejection(db: Session, world: World) -> None:
    book = _book(db)
    _seed_events(db, world, book)
    assert open_followups(db, book, world.days[EVENT], world.market, world.calendar, 1) == 3
    followups = _followups(db)
    assert {(f.event, f.symbol) for f in followups} == {
        ("exit", "600002"),
        ("abandoned", "600003"),
        ("rejected", "600004"),
    }
    assert all(f.user_id == book.user_id and f.book_id == book.id for f in followups)
    assert all(f.base_adj_close == pytest.approx(10.0) for f in followups)
    assert all(f.ret_20 is None and f.ret_60 is None for f in followups)


def test_followup_not_duplicated_on_rerun_or_repeated_rejection(db: Session, world: World) -> None:
    book = _book(db)
    _seed_events(db, world, book)
    day = world.days[EVENT]
    assert open_followups(db, book, day, world.market, world.calendar, 1) == 3
    assert open_followups(db, book, day, world.market, world.calendar, 1) == 0
    db.commit()
    assert len(_followups(db)) == 3
    # 同一只股票后来又被否决：合并进原事件（只延长 last_day），首日不是当日，不新建
    for r in db.scalars(select(RejectionRow).where(RejectionRow.symbol == "600004")):
        r.last_day = world.days[EVENT + 5]
    db.flush()
    assert open_followups(db, book, world.days[EVENT + 5], world.market, world.calendar, 5) == 0
    assert len(_followups(db)) == 3


def test_followup_base_uses_last_price_when_suspended_and_skips_without_price(
    db: Session, world: World
) -> None:
    book = _book(db)
    d = world.days
    _trade(db, book, "600003", d[EVENT + 20])  # 当日停牌：取此前最后一个复权价
    _trade(db, book, "600005", d[EVENT])  # 此前一个价格都没有
    db.commit()
    assert open_followups(db, book, d[EVENT + 20], world.market, world.calendar, 1) == 1
    assert open_followups(db, book, d[EVENT], world.market, world.calendar, 1) == 0
    [f] = _followups(db)
    assert (f.symbol, f.event_day, f.base_adj_close) == ("600003", d[EVENT + 20], 10.0)


def _opened(
    db: Session, w: World, symbol: str, event: str = "exit", email: str = "j@example.com"
) -> Followup:
    book = _book(db, email)
    add: dict[str, Callable[[], None]] = {
        "exit": lambda: _trade(db, book, symbol, w.days[EVENT]),
        "rejected": lambda: _rejection(db, book, symbol, w.days[EVENT], w.days[EVENT]),
    }
    add[event]()
    db.commit()
    open_followups(db, book, w.days[EVENT], w.market, w.calendar, 1)
    db.commit()
    return _followups(db)[-1]


def test_ret_20_and_60_filled_on_their_days_with_adjusted_prices(db: Session, world: World) -> None:
    f = _opened(db, world, "600002")
    d, cal, m = world.days, world.calendar, world.market
    assert fill_followups(db, cal, m, d[EVENT + 20], [20, 60]) == 1
    db.commit()
    db.refresh(f)
    assert f.ret_20 == pytest.approx(0.10) and f.ret_60 is None
    # 第 60 个交易日在除权日（未复权价减半）之后：复权价 9.0，收益 -10%
    assert fill_followups(db, cal, m, d[EVENT + 60], [20, 60]) == 1
    db.commit()
    db.refresh(f)
    assert f.ret_20 == pytest.approx(0.10) and f.ret_60 == pytest.approx(-0.10)
    assert fill_followups(db, cal, m, d[EVENT + 61], [20, 60]) == 0  # 已填的不再动


def test_followup_waits_when_horizon_not_reached(db: Session, world: World) -> None:
    f = _opened(db, world, "600002")
    assert fill_followups(db, world.calendar, world.market, world.days[EVENT + 19], [20, 60]) == 0
    db.refresh(f)
    assert f.ret_20 is None and f.ret_60 is None


def test_followup_uses_next_available_price_when_suspended(db: Session, world: World) -> None:
    f = _opened(db, world, "600003")
    d, cal, m = world.days, world.calendar, world.market
    assert fill_followups(db, cal, m, d[EVENT + 20], [20]) == 0  # 到期日停牌、其后还没有价格
    assert fill_followups(db, cal, m, d[EVENT + 21], [20]) == 1
    db.refresh(f)
    assert f.ret_20 == pytest.approx(0.20)


def test_followup_left_empty_when_delisted(db: Session, world: World) -> None:
    f = _opened(db, world, "600004", "rejected")
    assert fill_followups(db, world.calendar, world.market, world.days[EVENT + 70], [20, 60]) == 0
    db.refresh(f)
    assert f.ret_20 is None and f.ret_60 is None


def test_fill_covers_every_user_and_rejects_unsupported_horizon(db: Session, world: World) -> None:
    first = _opened(db, world, "600002")
    second = _opened(db, world, "600002", "rejected", "k@example.com")
    assert first.user_id != second.user_id
    d = world.days
    with pytest.raises(ValueError, match="跟踪天数"):
        fill_followups(db, world.calendar, world.market, d[EVENT + 20], [20, 30])
    assert fill_followups(db, world.calendar, world.market, d[EVENT + 20], [20]) == 2


def test_store_market_adj_close(world: World) -> None:
    m, d = world.market, world.days
    assert m.adj_close(d[EX - 1], "600002") == pytest.approx(10.0)
    assert m.adj_close(d[EX], "600002") == pytest.approx(10.0)  # 除权后未复权价减半，复权价不变
    assert m.adj_close(d[EVENT + 20], "600003") is None  # 停牌
    assert m.adj_close(date(2024, 1, 6), "600002") is None  # 非交易日
    assert m.adj_close(d[0], "999999") is None


def test_late_recorded_exit_gets_followup_with_real_exit_day_and_window_is_bounded(
    db: Session, world: World
) -> None:
    book = _book(db)
    d, m, cal = world.days, world.market, world.calendar
    _trade(db, book, "600002", d[EVENT - 1])  # 前一晚的卖出，今晚才录入
    _trade(db, book, "600003", d[EVENT - 9])  # 窗口（5 个交易日）之外
    db.commit()
    assert open_followups(db, book, d[EVENT], m, cal, 5) == 1
    [f] = _followups(db)
    assert (f.event, f.symbol, f.event_day) == ("exit", "600002", d[EVENT - 1])
    assert open_followups(db, book, d[EVENT], m, cal, 5) == 0  # 重跑不新建
    assert open_followups(db, book, d[EVENT + 1], m, cal, 5) == 0  # 次晚窗口重叠也不重复
    assert len(_followups(db)) == 1


def test_old_unfilled_followups_are_not_listed_forever(db: Session, world: World) -> None:
    f = _opened(db, world, "600004", "rejected")  # 退市，永远填不上
    d, cal, m = world.days, world.calendar, world.market
    assert fill_followups(db, cal, m, d[EVENT + 20 + 30], [20]) == 0  # 窗口内仍会查
    assert fill_followups(db, cal, m, d[EVENT + 20 + 31], [20]) == 0  # 超出窗口：不再查
    db.refresh(f)
    assert f.ret_20 is None
    assert admin_repo.list_unfilled_followups(db, d[EVENT + 1], d[-1]) == []
    assert admin_repo.list_unfilled_followups(db, d[EVENT], d[-1]) == [f]
