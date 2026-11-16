"""只读查询 qt.service.views：跑两个交易日后，各视图的关键数字与 manual_state、数据库行一致；
别人的订阅编号一律 NotFoundError；已关闭的订阅只读。"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from qt.backtest.state import load_state
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import DailyRun, Followup, Rejection, Signal, Subscription
from qt.models import Position
from qt.service import accounts, views
from qt.service.accounts import create_user, subscription_config
from qt.service.actor import CLI
from qt.service.errors import NotFoundError
from qt.service.manual_ops import manual_state
from qt.service.market import StoreMarket, build_store_market
from qt.strategy.high_dividend_low import exit_levels
from tests.fakes import populate_trading_store
from tests.jobs.conftest import CASH, CFG, DAY, Env, FakeClock, evening, market_on, run_two_days


@pytest.fixture
def env(tmp_path: Path, db_engine: Engine, db: Session) -> Iterator[Env]:
    store = ParquetStore(tmp_path / "data")
    days = populate_trading_store(store)
    yield Env(db_engine, store, days, FakeClock(evening(days[DAY])))


def _user(db: Session, email: str = "a@example.com") -> int:
    user = create_user(db, email, actor=CLI)
    db.commit()
    return user.id


def _market(env: Env, db: Session, sub: Subscription) -> StoreMarket:
    """只覆盖该订阅页面所需股票的行情（与网页进程的取法相同）。"""
    symbols = views.book_symbols(db, sub.user_id, sub.id)
    return build_store_market(env.store, CFG, env.calendar, env.days[DAY + 1], symbols)


def _signals(db: Session, sub: Subscription, kind: str) -> list[Signal]:
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, kind)
    return repo.list_signals(db, sub.user_id, book.id)


@pytest.fixture
def ran(env: Env, db: Session) -> Subscription:
    return run_two_days(env, db, _user(db))


def test_latest_run_and_its_data_state(env: Env, db: Session) -> None:
    assert views.latest_run(db) is None
    moment = datetime(2022, 1, 4, 10, 0, tzinfo=UTC)
    db.add(
        DailyRun(
            trade_day=date(2022, 1, 4),
            status="data_failed",
            stage="data",
            started_at=moment,
            finished_at=moment,
            detail={"error": "证券列表取不到"},
        )
    )
    db.commit()
    run = views.latest_run(db)
    assert run is not None and run.day == date(2022, 1, 4) and not run.data_ok
    assert run.status == "data_failed" and run.finished_at == moment


def test_book_symbols_cover_ledger_system_positions_and_open_signals(
    env: Env, db: Session, ran: Subscription
) -> None:
    symbols = views.book_symbols(db, ran.user_id, ran.id)
    system = repo.get_book_of_kind(db, ran.user_id, ran.id, "system")
    state_row = repo.get_system_state(db, ran.user_id, system.id)
    assert state_row is not None
    held = set(load_state(state_row.state)[0].positions)
    open_manual = {s.symbol for s in _signals(db, ran, "manual") if s.status == "notified"}
    assert held and open_manual
    assert symbols == frozenset(held | open_manual | {"600001"})


def test_overview_numbers_match_books_and_todays_signals(
    env: Env, db: Session, ran: Subscription
) -> None:
    today = env.days[DAY + 1]
    market = _market(env, db, ran)
    view = views.overview(db, ran.user_id, market, env.calendar, CFG, today)
    assert view.run is not None and view.run.day == today and view.run.data_ok
    assert view.subscription is not None and view.subscription.id == ran.id
    assert not view.subscription.read_only

    state = manual_state(db, ran.user_id, ran.id, market, env.calendar, CFG, today)
    assert view.manual is not None
    assert view.manual.nav == state.equity[-1] and view.manual.cash == state.cash
    assert view.manual.holdings_count == len(state.positions) == 1
    assert view.manual.holdings_value == pytest.approx(state.equity[-1] - state.cash)
    assert view.manual.as_of == today

    system = repo.get_book_of_kind(db, ran.user_id, ran.id, "system")
    last = repo.list_book_days(db, ran.user_id, system.id)[-1]
    assert view.system is not None
    assert (view.system.nav, view.system.cash, view.system.as_of) == (
        last.equity,
        last.cash,
        today,
    )
    assert view.system.unit_nav == last.unit_nav

    todays = {
        s.id
        for kind in ("manual", "system")
        for s in _signals(db, ran, kind)
        if s.signal_day == today
    }
    assert {row.id for row in view.todays_signals} == todays


def test_overview_without_subscription(env: Env, db: Session) -> None:
    user_id = _user(db)
    view = views.overview(db, user_id, None, env.calendar, CFG, env.days[DAY])
    assert view.subscription is None and view.manual is None and view.system is None
    assert view.run is None and view.todays_signals == []


def test_signals_page_splits_pending_history_and_rejections(
    env: Env, db: Session, ran: Subscription
) -> None:
    today = env.days[DAY + 1]
    names = {"600002": "二号股份"}
    view = views.signals_page(db, ran.user_id, ran.id, env.calendar, CFG, today, names=names)
    manual = _signals(db, ran, "manual")
    system = _signals(db, ran, "system")
    pending = {s.id for s in manual if s.status in ("created", "notified")}
    assert pending and {r.id for r in view.pending} == pending
    assert all(r.book == "manual" and r.can_record for r in view.pending)
    assert {r.id for r in view.history} == {s.id for s in [*manual, *system]} - pending
    executed = next(r for r in view.history if r.book == "manual" and r.symbol == "600001")
    assert executed.status == "executed" and executed.status_label == "已执行"
    assert not executed.can_record
    assert not any(r.can_record for r in view.history)
    assert next(r for r in view.pending if r.symbol == "600002").name == "二号股份"
    first = next(s for s in manual if s.symbol == "600002")
    row = next(r for r in view.pending if r.id == first.id)
    assert (row.side, row.shares, row.ref_price, row.signal_day, row.valid_until) == (
        "buy",
        first.shares,
        first.ref_price,
        first.signal_day,
        first.valid_until,
    )
    count = len(db.scalars(select(Rejection.id).where(Rejection.user_id == ran.user_id)).all())
    assert len(view.rejections) == count


def test_buy_signal_past_its_validity_cannot_be_recorded(
    env: Env, db: Session, ran: Subscription
) -> None:
    late = env.days[DAY + 1 + CFG.service.signal_valid_days + 1]
    view = views.signals_page(db, ran.user_id, ran.id, env.calendar, CFG, late)
    assert view.pending and not any(r.can_record for r in view.pending)


def test_system_book_holdings_entries_and_nav_series(
    env: Env, db: Session, ran: Subscription
) -> None:
    today = env.days[DAY + 1]
    market = market_on(env, today)
    view = views.system_book(db, ran.user_id, ran.id, market)
    book = repo.get_book_of_kind(db, ran.user_id, ran.id, "system")
    state_row = repo.get_system_state(db, ran.user_id, book.id)
    assert state_row is not None
    positions = load_state(state_row.state)[0].positions
    assert [h.symbol for h in view.holdings] == list(positions)
    for h in view.holdings:
        pos = positions[h.symbol]
        close = market.last_close(today, h.symbol)
        assert (h.shares, h.cost, h.close) == (pos.shares, pos.cost, close)
        assert h.value == pos.shares * close and h.ret == pos.ret(close)
        assert h.origin_label == "按信号买入"
    rows = repo.list_ledger_entries(db, ran.user_id, book.id)
    assert [e.id for e in view.entries] == [r.id for r in rows] and rows
    assert not any(e.can_void for e in view.entries)
    days = repo.list_book_days(db, ran.user_id, book.id)
    assert [(p.day, p.equity, p.unit_nav) for p in view.nav] == [
        (d.day, d.equity, d.unit_nav) for d in days
    ]
    assert view.summary is not None and view.summary.holdings_count == len(positions)


def test_system_book_before_first_run(env: Env, db: Session) -> None:
    user_id = _user(db)
    sub = accounts.create_subscription(db, user_id, CFG, CASH, env.days[DAY], None, actor=CLI)
    db.commit()
    view = views.system_book(db, user_id, sub.id)
    assert view.summary is None and view.holdings == [] and view.entries == []


def test_manual_book_matches_manual_state(env: Env, db: Session, ran: Subscription) -> None:
    today = env.days[DAY + 1]
    market = _market(env, db, ran)
    view = views.manual_book(db, ran.user_id, ran.id, market, env.calendar, CFG, today)
    state = manual_state(db, ran.user_id, ran.id, market, env.calendar, CFG, today)
    assert view.summary.cash == state.cash and view.summary.nav == state.equity[-1]
    (holding,) = view.holdings
    pos = state.positions["600001"]
    assert (holding.symbol, holding.shares, holding.cost) == ("600001", pos.shares, pos.cost)
    assert holding.cost_price == pos.cost / pos.shares
    close = market.last_close(today, "600001")
    assert holding.close == close and holding.pnl == pytest.approx(
        pos.shares * close + pos.cash_received - pos.cost
    )
    assert holding.origin_label == "按信号买入"
    strategy = subscription_config(CFG, ran).strategy
    assert (holding.stop_price, holding.take_price) == exit_levels(pos, strategy)
    assert holding.stop_price is not None and holding.take_price is not None

    book = repo.get_book_of_kind(db, ran.user_id, ran.id, "manual")
    rows = repo.list_ledger_entries(db, ran.user_id, book.id)
    assert [e.id for e in view.entries] == [r.id for r in rows]
    assert [e.kind for e in view.entries] == ["buy", "sell"]
    assert all(e.can_void for e in view.entries)
    assert view.entries[0].signal_id is not None and view.entries[0].origin == "signal"


def test_exit_levels_are_where_the_strategy_rule_triggers() -> None:
    strategy = CFG.strategy
    pos = Position("600001", 1000.0, 10_000.0, date(2022, 1, 4), "", {}, cash_received=300.0)
    stop, take = exit_levels(pos, strategy)
    assert stop is not None and take is not None
    assert pos.ret(stop) == pytest.approx(-strategy.stop_loss)
    assert pos.ret(take) == pytest.approx(strategy.take_profit_fixed)
    trailing = strategy.model_copy(update={"take_profit_mode": "trailing"})
    assert exit_levels(pos, trailing)[1] is None  # 还没涨到启动涨幅
    pos.peak_return = trailing.trailing_activate + 0.1
    level = exit_levels(pos, trailing)[1]
    assert level is not None
    drawdown = (1 + pos.ret(level)) / (1 + pos.peak_return) - 1
    assert drawdown == pytest.approx(-trailing.trailing_drawdown)


def test_journal_lists_trades_and_followups_of_both_books(
    env: Env, db: Session, ran: Subscription
) -> None:
    book = repo.get_book_of_kind(db, ran.user_id, ran.id, "manual")
    filled = db.scalars(select(Followup).where(Followup.book_id == book.id)).one()
    filled.ret_20 = 0.05  # 离场时由每日任务建好，20 日涨跌在这里先填上
    db.commit()
    view = views.journal(db, ran.user_id, ran.id)
    trades = repo.list_trades(db, ran.user_id, book.id)
    assert len(trades) == 1
    (row,) = view.manual.trades
    t = trades[0]
    assert (row.symbol, row.shares, row.cost, row.exit_day) == (
        t.symbol,
        t.shares,
        t.cost,
        t.exit_day,
    )
    assert row.received == t.proceeds + t.cash_received
    assert row.ret == (t.proceeds + t.cash_received) / t.cost - 1
    assert row.exit_label == "自主卖出" and row.origin_label == "按信号买入"
    (f,) = view.manual.followups
    assert (f.symbol, f.event_label, f.event_day) == ("600001", "离场后", env.days[DAY + 1])
    assert (f.ret_20, f.ret_60) == (0.05, None)
    assert view.system.trades == [] and view.system.followups == []


def test_foreign_subscription_ids_raise_not_found(env: Env, db: Session, ran: Subscription) -> None:
    other = _user(db, "b@example.com")
    with pytest.raises(NotFoundError):
        views.resolve_subscription(db, other, ran.id)
    today = env.days[DAY + 1]
    market = market_on(env, today)
    calls = [
        lambda: views.signals_page(db, other, ran.id, env.calendar, CFG, today),
        lambda: views.system_book(db, other, ran.id),
        lambda: views.manual_book(db, other, ran.id, market, env.calendar, CFG, today),
        lambda: views.journal(db, other, ran.id),
        lambda: views.book_symbols(db, other, ran.id),
        lambda: views.overview(db, other, market, env.calendar, CFG, today, subscription_id=ran.id),
    ]
    for call in calls:
        with pytest.raises(NotFoundError):
            call()
    assert views.resolve_subscription(db, other, None) is None
    shown = views.resolve_subscription(db, ran.user_id, None)
    assert shown is not None and shown.id == ran.id


def test_closed_subscription_is_read_only(env: Env, db: Session, ran: Subscription) -> None:
    accounts.close_subscription(db, ran.user_id, ran.id, actor=CLI)
    db.commit()
    today = env.days[DAY + 1]
    assert views.resolve_subscription(db, ran.user_id, ran.id) is not None
    signals = views.signals_page(db, ran.user_id, ran.id, env.calendar, CFG, today)
    assert signals.subscription.read_only and signals.subscription.status_label == "已关闭"
    assert not any(r.can_record for r in [*signals.pending, *signals.history])
    market = _market(env, db, ran)
    manual = views.manual_book(db, ran.user_id, ran.id, market, env.calendar, CFG, today)
    assert manual.subscription.read_only and not any(e.can_void for e in manual.entries)
    listed = views.subscriptions(db, ran.user_id)
    assert [s.id for s in listed] == [ran.id] and listed[0].read_only


def test_recordable_signal_tells_owner_handled_from_foreign(
    env: Env, db: Session, ran: Subscription
) -> None:
    """可录入返回该行；本人的但已不能录入返回 None；别人的、不存在的抛 NotFoundError。"""
    today = env.days[DAY + 1]
    manual = _signals(db, ran, "manual")
    open_buy = next(s for s in manual if s.status in ("created", "notified") and s.side == "buy")
    executed = next(s for s in manual if s.status == "executed")
    system_sig = _signals(db, ran, "system")[0]
    uid = ran.user_id
    row = views.recordable_signal(db, uid, ran.id, open_buy.id, env.calendar, CFG, today)
    assert row is not None and row.id == open_buy.id
    for sig_id in (executed.id, system_sig.id):
        assert views.recordable_signal(db, uid, ran.id, sig_id, env.calendar, CFG, today) is None
    late = env.days[DAY + 1 + CFG.service.signal_valid_days + 1]
    assert views.recordable_signal(db, uid, ran.id, open_buy.id, env.calendar, CFG, late) is None
    # 订阅关闭后没有开着的订阅：本人的信号一律算"已处理"
    assert views.recordable_signal(db, uid, None, open_buy.id, env.calendar, CFG, today) is None
    other = _user(db, "b@example.com")
    for sig_id in (open_buy.id, executed.id, 999999):
        with pytest.raises(NotFoundError):
            views.recordable_signal(db, other, None, sig_id, env.calendar, CFG, today)
    with pytest.raises(NotFoundError):
        views.recordable_signal(db, uid, ran.id, 999999, env.calendar, CFG, today)
