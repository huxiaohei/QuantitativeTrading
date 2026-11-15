"""手动账的录入与每晚推进：信号的生成、关联、到期、提醒、作废，以及派生表的重写。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.costs import LOT_SIZE, CostModel
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import Base, Book, BookDay, LedgerEntry, Signal, Subscription, Trade
from qt.db.models import Rejection as RejectionRow
from qt.features import build_panel
from qt.models import Side
from qt.service.accounts import create_subscription, create_user
from qt.service.errors import NotFoundError, ValidationError
from qt.service.manual_ops import (
    ManualDayResult,
    advance_manual_book,
    manual_state,
    record_cash,
    record_holding,
    record_trade,
    void_entry,
)
from qt.service.market import StoreMarket
from tests.fakes import TRADING_SYMBOLS, make_industries, make_securities, populate_trading_store

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
CASH = 100_000.0
S = 260  # 订阅起始日的交易日序号：此时四只股票都在 10 元附近、满足买入条件
SUSPEND = (265, 266)  # 变体数据中 600001 停牌的交易日序号
DELIST = 270  # 变体数据中 600002 的退市日序号


@dataclass(frozen=True)
class World:
    """一份数据集：存储、日历、指标面板、行情与全部交易日。"""

    store: ParquetStore
    calendar: TradingCalendar
    market: StoreMarket
    days: list[date]
    cfg: AppConfig


def _world(store: ParquetStore, days: list[date]) -> World:
    calendar = TradingCalendar(days)
    panel = build_panel(store, calendar, CFG, days[0], days[-1])
    return World(store, calendar, StoreMarket(store, panel, CFG), days, CFG)


def _make_variant(store: ParquetStore) -> list[date]:
    """交易数据集的变体：600004 改为与 600003 同一行业（电力）；600001 停牌两天；600002 退市。"""
    days = populate_trading_store(store)
    store.write(
        Table.INDUSTRIES,
        make_industries(
            [
                ("600001", "银行", False),
                ("600002", "煤炭", False),
                ("600003", "电力", False),
                ("600004", "电力", False),
            ]
        ),
    )
    bars1 = store.read(Table.BARS, symbols=["600001"])
    suspended = {pd.Timestamp(days[i]) for i in SUSPEND}
    store.write(
        Table.BARS, bars1[~bars1["day"].isin(suspended)].reset_index(drop=True), symbol="600001"
    )
    bars2 = store.read(Table.BARS, symbols=["600002"])
    kept = bars2[bars2["day"] < pd.Timestamp(days[DELIST])].reset_index(drop=True)
    store.write(Table.BARS, kept, symbol="600002")
    rows = [
        (s, "sh_main", date(2010, 1, 1), days[DELIST] if s == "600002" else None)
        for s in TRADING_SYMBOLS
    ]
    store.write(Table.SECURITIES, make_securities(rows))
    return days


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> World:
    store = ParquetStore(tmp_path_factory.mktemp("manual_plain"))
    return _world(store, populate_trading_store(store))


@pytest.fixture(scope="module")
def variant(tmp_path_factory: pytest.TempPathFactory) -> World:
    store = ParquetStore(tmp_path_factory.mktemp("manual_variant"))
    return _world(store, _make_variant(store))


def _subscribe(
    db: Session,
    w: World,
    overrides: dict[str, object] | None = None,
    email: str = "manual@example.com",
) -> Subscription:
    user = create_user(db, email)
    sub = create_subscription(db, user.id, CFG, CASH, w.days[S], overrides)
    db.commit()
    return sub


def _manual_book(db: Session, sub: Subscription) -> Book:
    return repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")


def _night(
    db: Session, sub: Subscription, w: World, i: int, notify: bool = True
) -> ManualDayResult:
    result = advance_manual_book(
        db, sub, w.cfg, w.market.panel, w.market, w.calendar, w.days[i], notify
    )
    db.commit()
    db.expire_all()  # 下一步一切从数据库重新读取
    return result


def _nights(
    db: Session, sub: Subscription, w: World, first: int, last: int
) -> dict[int, ManualDayResult]:
    return {i: _night(db, sub, w, i) for i in range(first, last + 1)}


def _holding(
    db: Session, sub: Subscription, w: World, symbol: str, cost: float, shares: float, today: int
) -> LedgerEntry:
    entry = record_holding(
        db,
        sub.user_id,
        sub.id,
        w.market,
        w.calendar,
        w.cfg,
        w.days[today],
        symbol,
        w.days[today - 30],
        cost,
        shares,
    )
    db.commit()
    return entry


def _trade(
    db: Session,
    sub: Subscription,
    w: World,
    side: Side,
    symbol: str,
    day: int,
    price: float,
    shares: float,
    today: int | None = None,
    fees: float | None = None,
) -> LedgerEntry:
    entry = record_trade(
        db,
        sub.user_id,
        sub.id,
        w.market,
        w.calendar,
        w.cfg,
        w.days[day if today is None else today],
        side,
        symbol,
        w.days[day],
        price,
        shares,
        fees,
    )
    db.commit()
    return entry


def _signals(db: Session, book: Book, side: str | None = None) -> list[Signal]:
    stmt = select(Signal).where(Signal.book_id == book.id)
    if side is not None:
        stmt = stmt.where(Signal.side == side)
    return list(db.scalars(stmt.order_by(Signal.id)))


def _row_counts(db: Session) -> dict[str, int]:
    return {
        t.name: db.scalar(select(func.count()).select_from(t)) or 0
        for t in Base.metadata.sorted_tables
    }


def _columns(row: Signal) -> tuple[Any, ...]:
    return tuple(getattr(row, c.key) for c in Signal.__table__.columns)


def _symbols(signals: Sequence[Signal]) -> list[str]:
    return [s.symbol for s in signals]


# ---------- 买入信号 ----------


def test_buy_signal_sized_from_manual_book_cash(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    day = plain.days[S]
    result = _night(db, sub, plain, S)
    [sig] = [s for s in result.new_buy_signals if s.symbol == "600001"]
    # 总资产 100000、单只上限 10% → 预算 10000 元，收盘 10.00，扣费后向下取整到一手
    expected = CostModel(CFG.costs).max_lots(10.0, CASH * 0.1) * LOT_SIZE
    assert sig.shares == expected and expected > 0
    assert sig.ref_price == 10.0 and sig.signal_day == day and sig.side == "buy"
    assert sig.status == "notified" and sig.valid_until == plain.calendar.offset(day, 3)
    assert sig.stop_price == pytest.approx(10.0 * 0.85) and sig.take_price == pytest.approx(12.0)
    assert sig.metrics["dividend_yield"] == pytest.approx(0.06)
    assert _manual_book(db, sub).processed_through == day
    [book_day] = db.scalars(select(BookDay).where(BookDay.book_id == sig.book_id))
    assert (book_day.day, book_day.equity, book_day.cash, book_day.unit_nav) == (
        day,
        CASH,
        CASH,
        1.0,
    )


def test_recording_buy_within_validity_links_signal(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    [sig] = [s for s in _night(db, sub, plain, S).new_buy_signals if s.symbol == "600001"]
    e = _trade(db, sub, plain, Side.BUY, "600001", S + 1, 10.05, 1000)
    assert e.origin == "signal" and e.signal_id == sig.id
    assert e.fees_estimated and e.fees > 0 and e.kind == "buy"
    assert "请核对" in e.note  # 假数据当日最高最低价都是 10.00
    db.refresh(sig)
    assert sig.status == "executed" and sig.status_day == plain.days[S + 1]


def test_buy_without_signal_is_self_directed(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    e = _trade(db, sub, plain, Side.BUY, "600004", S, 9.9, 500)
    assert e.origin == "self" and e.signal_id is None
    # 600004 当日最高最低价都是 10.20：提示但允许保存
    assert "请核对" in e.note and "最低价" in e.note


def test_explicit_fees_are_kept_and_cash_entries_recorded(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    e = _trade(db, sub, plain, Side.BUY, "600003", S, 10.0, 1000, fees=7.5)
    assert e.fees == 7.5 and not e.fees_estimated
    dep = record_cash(
        db,
        sub.user_id,
        sub.id,
        plain.market,
        plain.calendar,
        plain.cfg,
        plain.days[S],
        "deposit",
        plain.days[S],
        5000.0,
    )
    assert dep.kind == "deposit" and dep.price == 5000.0 and dep.symbol is None
    state = manual_state(
        db, sub.user_id, sub.id, plain.market, plain.calendar, plain.cfg, plain.days[S]
    )
    assert state.cash == pytest.approx(CASH - 10_000 - 7.5 + 5000)
    assert state.positions["600003"].shares == 1000


def test_sell_same_day_as_buy_gets_t1_note(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _trade(db, sub, plain, Side.BUY, "600003", S, 10.0, 1000)
    e = _trade(db, sub, plain, Side.SELL, "600003", S, 10.0, 1000)
    assert e.origin == "self" and "T+1" in e.note


def test_invalid_entry_is_rejected_without_writing(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    before = _row_counts(db)
    with pytest.raises(ValidationError, match="超过持仓"):
        _trade(db, sub, plain, Side.SELL, "600003", S, 10.0, 100)
    with pytest.raises(ValidationError, match="超过现金"):
        _trade(db, sub, plain, Side.BUY, "600003", S, 10.0, 20_000)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    with pytest.raises(ValidationError, match="已在持仓中"):
        _holding(db, sub, plain, "600003", 12.0, 1000, S)
    after = _row_counts(db)
    assert after["ledger_entries"] == before["ledger_entries"] + 1


def test_unacted_buy_signal_reserves_cash_and_slot(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain, {"risk.max_positions": 1})
    first = _night(db, sub, plain, S)
    assert _symbols(first.new_buy_signals) == ["600001"]
    second = _night(db, sub, plain, S + 1)
    assert second.new_buy_signals == []
    book = _manual_book(db, sub)
    rules = set(db.scalars(select(RejectionRow.rule).where(RejectionRow.book_id == book.id)))
    assert "max_positions" in rules


def test_buy_signal_abandoned_after_three_trading_days_then_blocked_for_20(
    db: Session, plain: World
) -> None:
    sub = _subscribe(db, plain)
    results = _nights(db, sub, plain, S, S + 25)
    assert "600003" in _symbols(results[S].new_buy_signals)
    for i in (S + 1, S + 2, S + 3):
        assert results[i].abandoned == [] and results[i].new_buy_signals == []
    # 第 4 个交易日晚上：有效期（3 个交易日）已过 → 放弃
    abandoned = results[S + 4].abandoned
    assert "600003" in _symbols(abandoned)
    assert all(s.status == "abandoned" for s in abandoned)
    # 之后 20 个交易日内不再出信号，第 21 个交易日可以
    for i in range(S + 4, S + 25):
        assert "600003" not in _symbols(results[i].new_buy_signals), i
    assert "600003" in _symbols(results[S + 25].new_buy_signals)


# ---------- 卖出信号 ----------


def test_sell_signal_emitted_once_reminded_once_then_silent(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    results = _nights(db, sub, plain, S, S + 7)
    [sig] = results[S].new_sell_signals
    assert (sig.symbol, sig.exit_reason, sig.status) == ("600003", "stop_loss", "notified")
    assert sig.valid_until is None and sig.ref_price == 10.0 and sig.shares == 1000
    for i in range(S + 1, S + 8):
        assert results[i].new_sell_signals == [], i
    for i in (S + 1, S + 2, *range(S + 4, S + 8)):
        assert results[i].reminders == [], i
    [reminded] = results[S + 3].reminders
    assert reminded.id == sig.id and reminded.reminded_day == plain.days[S + 3]
    db.refresh(sig)
    assert sig.status == "notified"


def test_sell_signal_voided_when_condition_no_longer_holds(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    # 600002 自第 262 天起从 10 元跌到第 290 天的 7 元：成本 8.30 时先达止盈 20%，随后回落，最后止损
    _holding(db, sub, plain, "600002", 8.3, 1000, S)
    results = _nights(db, sub, plain, S, S + 30)
    [first] = results[S].new_sell_signals
    assert first.exit_reason == "take_profit_fixed"
    db.refresh(first)
    assert first.status == "voided" and first.void_note == "条件不再满足"
    assert first.status_day == plain.days[S + 3]
    news = [(i, s.exit_reason) for i, r in results.items() for s in r.new_sell_signals]
    assert news == [(S, "take_profit_fixed"), (S + 30, "stop_loss")]


def test_recording_sell_links_signal_and_starts_cooldown(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    [sig] = _night(db, sub, plain, S).new_sell_signals
    e = _trade(db, sub, plain, Side.SELL, "600003", S + 1, 10.0, 1000)
    assert e.origin == "signal" and e.signal_id == sig.id
    db.refresh(sig)
    assert sig.status == "executed"
    _night(db, sub, plain, S + 1)
    book = _manual_book(db, sub)
    [trade] = db.scalars(select(Trade).where(Trade.book_id == book.id))
    assert trade.exit_reason == "stop_loss" and trade.signal_ref_price == 10.0
    assert trade.origin == "self" and trade.fill_price == 10.0 and trade.shares == 1000
    rules = list(
        db.scalars(
            select(RejectionRow.rule).where(
                RejectionRow.book_id == book.id, RejectionRow.symbol == "600003"
            )
        )
    )
    assert rules == ["cooldown"]


def _cooldown_rules(db: Session, book: Book, symbol: str) -> list[str]:
    stmt = select(RejectionRow.rule).where(
        RejectionRow.book_id == book.id, RejectionRow.symbol == symbol
    )
    return list(db.scalars(stmt))


def test_void_and_rerecord_keeps_signal_link_and_cooldown(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    [sig] = _night(db, sub, plain, S).new_sell_signals
    wrong = _trade(db, sub, plain, Side.SELL, "600003", S + 1, 10.1, 1000)
    assert wrong.signal_id == sig.id
    void_entry(
        db,
        sub.user_id,
        sub.id,
        plain.market,
        plain.calendar,
        plain.cfg,
        plain.days[S + 1],
        wrong.id,
    )
    db.commit()
    # 更正后重新录入：仍关联原信号（信号保持已执行，不再改状态）
    fixed = _trade(db, sub, plain, Side.SELL, "600003", S + 1, 10.0, 1000)
    assert fixed.origin == "signal" and fixed.signal_id == sig.id
    db.refresh(sig)
    assert sig.status == "executed" and sig.status_day == plain.days[S + 1]
    _night(db, sub, plain, S + 1)
    book = _manual_book(db, sub)
    [trade] = db.scalars(select(Trade).where(Trade.book_id == book.id))
    assert trade.exit_reason == "stop_loss" and trade.fill_price == 10.0
    assert _cooldown_rules(db, book, "600003") == ["cooldown"]


def test_voided_sell_does_not_link_stale_signal_after_bound(db: Session, plain: World) -> None:
    # 作废已关联的止损卖出、不再重新录入；超过 作废流水的成交日 + 有效天数 之后的卖出不关联旧信号
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    [sig] = _night(db, sub, plain, S).new_sell_signals
    linked = _trade(db, sub, plain, Side.SELL, "600003", S + 1, 10.0, 1000)
    assert linked.signal_id == sig.id
    void_entry(
        db,
        sub.user_id,
        sub.id,
        plain.market,
        plain.calendar,
        plain.cfg,
        plain.days[S + 1],
        linked.id,
    )
    db.commit()
    bound = plain.calendar.offset(plain.days[S + 1], CFG.service.signal_valid_days)
    assert plain.days[S + 8] > bound
    e = _trade(db, sub, plain, Side.SELL, "600003", S + 8, 10.0, 1000)
    assert e.origin == "self" and e.signal_id is None
    state = manual_state(
        db, sub.user_id, sub.id, plain.market, plain.calendar, plain.cfg, plain.days[S + 8]
    )
    [trade] = state.trades
    assert trade.exit_reason is None
    assert state.risk.exits == ()  # 没有进入冷却


def test_void_and_rerecord_buy_keeps_signal_link(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    [sig] = [s for s in _night(db, sub, plain, S).new_buy_signals if s.symbol == "600001"]
    wrong = _trade(db, sub, plain, Side.BUY, "600001", S + 1, 10.0, 900)
    void_entry(
        db,
        sub.user_id,
        sub.id,
        plain.market,
        plain.calendar,
        plain.cfg,
        plain.days[S + 1],
        wrong.id,
    )
    db.commit()
    fixed = _trade(db, sub, plain, Side.BUY, "600001", S + 1, 10.0, 800)
    assert fixed.origin == "signal" and fixed.signal_id == sig.id


def test_late_sell_links_signal_voided_after_the_sale(db: Session, plain: World) -> None:
    # 成本 11.79：第 260 至 262 天收盘 10.00 时亏损 15.2%（止损），第 263 天价格回升到止损线以上
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600001", 11.79, 1000, S)
    results = _nights(db, sub, plain, S, S + 3)
    [sig] = results[S].new_sell_signals
    db.refresh(sig)
    assert sig.status == "voided" and sig.status_day == plain.days[S + 3]
    # 使用者在第 263 天（信号仍有效时）卖出，第 264 天才补录
    e = _trade(db, sub, plain, Side.SELL, "600001", S + 3, 10.05, 1000, today=S + 4)
    assert e.origin == "signal" and e.signal_id == sig.id
    db.refresh(sig)
    assert sig.status == "voided"  # 不改状态
    _night(db, sub, plain, S + 4)
    book = _manual_book(db, sub)
    [trade] = db.scalars(select(Trade).where(Trade.book_id == book.id))
    assert trade.exit_reason == "stop_loss" and trade.signal_ref_price == 10.0
    assert _cooldown_rules(db, book, "600001") == ["cooldown"]


def test_late_buy_links_abandoned_signal_within_validity(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    results = _nights(db, sub, plain, S, S + 4)
    [sig] = [s for s in results[S].new_buy_signals if s.symbol == "600001"]
    assert sig.id in {s.id for s in results[S + 4].abandoned}
    e = _trade(db, sub, plain, Side.BUY, "600001", S + 2, 10.0, 900, today=S + 4)
    assert e.origin == "signal" and e.signal_id == sig.id
    db.refresh(sig)
    assert sig.status == "abandoned"  # 不改状态
    state = manual_state(
        db, sub.user_id, sub.id, plain.market, plain.calendar, plain.cfg, plain.days[S + 4]
    )
    assert state.origins["600001"] == "signal"
    assert state.positions["600001"].entry_metrics["dividend_yield"] == pytest.approx(0.06)
    # 有效期之后的买入不关联
    late = _trade(db, sub, plain, Side.BUY, "600001", S + 4, 10.0, 100)
    assert late.origin == "self" and late.signal_id is None


def test_partial_sell_links_signal_and_rule_fires_again_for_rest(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    [sig] = _night(db, sub, plain, S).new_sell_signals
    e = _trade(db, sub, plain, Side.SELL, "600003", S + 1, 10.0, 400)
    assert e.signal_id == sig.id
    db.refresh(sig)
    assert sig.status == "executed"
    result = _night(db, sub, plain, S + 1)
    assert [(s.symbol, s.exit_reason, s.shares) for s in result.new_sell_signals] == [
        ("600003", "stop_loss", 600)
    ]
    book = _manual_book(db, sub)
    [trade] = db.scalars(select(Trade).where(Trade.book_id == book.id))
    assert (trade.shares, trade.exit_reason) == (400, "stop_loss")


def test_sell_of_transferred_holding_on_first_day_has_no_t1_note(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    e = _trade(db, sub, plain, Side.SELL, "600003", S, 10.0, 1000)
    assert e.note == ""


def test_self_directed_holding_gets_sell_checks_and_counts_in_risk(
    db: Session, variant: World
) -> None:
    # 变体数据中 600003、600004 同属电力
    sub = _subscribe(db, variant, {"risk.max_industry_pct": 0.15})
    _holding(db, sub, variant, "600003", 12.0, 2000, S)
    result = _night(db, sub, variant, S)
    assert [(s.symbol, s.exit_reason) for s in result.new_sell_signals] == [("600003", "stop_loss")]
    # 总资产 120000，电力上限 18000 元，已持有 600003 市值 20000 元 → 600004 被行业上限拒绝
    book = _manual_book(db, sub)
    [rej] = db.scalars(
        select(RejectionRow).where(RejectionRow.book_id == book.id, RejectionRow.symbol == "600004")
    )
    assert rej.rule == "industry_cap" and "电力" in rej.message
    assert "600001" in _symbols(result.new_buy_signals)


def test_pause_voids_open_buy_signals_and_stops_new_ones(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain, {"risk.pause_drawdown": 0.003, "risk.warn_drawdown": 0.001})
    first = _night(db, sub, plain, S)
    assert len(first.new_buy_signals) == 4
    # 以高于收盘价的价格买入，净值回撤约 0.45% → 当晚暂停
    _trade(db, sub, plain, Side.BUY, "600001", S + 1, 10.5, 900)
    second = _night(db, sub, plain, S + 1)
    assert [e.kind for e in second.risk_events] == ["pause"]
    assert second.new_buy_signals == []
    book = _manual_book(db, sub)
    voided = [s for s in _signals(db, book, "buy") if s.status == "voided"]
    assert sorted(_symbols(voided)) == ["600002", "600003", "600004"]
    assert {s.void_note for s in voided} == {"暂停买入时取消"}
    third = _night(db, sub, plain, S + 2)
    assert third.new_buy_signals == []
    rules = set(db.scalars(select(RejectionRow.rule).where(RejectionRow.book_id == book.id)))
    assert "paused" not in rules


def test_suspended_holding_valued_at_last_close(db: Session, variant: World) -> None:
    sub = _subscribe(db, variant)
    _holding(db, sub, variant, "600001", 10.0, 1000, S)
    _nights(db, sub, variant, S, SUSPEND[-1] + 1)
    book = _manual_book(db, sub)
    rows = {r.day: r for r in db.scalars(select(BookDay).where(BookDay.book_id == book.id))}
    last_close = variant.market.last_close(variant.days[SUSPEND[0] - 1], "600001")
    for i in SUSPEND:
        row = rows[variant.days[i]]
        assert row.cash == CASH
        assert row.equity == pytest.approx(CASH + 1000 * last_close)


def test_open_sell_signal_kept_while_holding_is_suspended(db: Session, variant: World) -> None:
    # 停牌当日无法判断卖出条件：已有的卖出信号保持原状，复牌后仍触发也不重复新建
    sub = _subscribe(db, variant)
    _holding(db, sub, variant, "600001", 12.5, 1000, S)
    results = _nights(db, sub, variant, S, SUSPEND[-1] + 1)
    [sig] = results[S].new_sell_signals
    assert sig.exit_reason == "stop_loss"
    assert all(results[i].new_sell_signals == [] for i in range(S + 1, SUSPEND[-1] + 2))
    db.refresh(sig)
    assert sig.status == "notified"


def test_delisted_holding_emits_delisted_sell_signal_once(db: Session, variant: World) -> None:
    sub = _subscribe(db, variant)
    _holding(db, sub, variant, "600002", 9.6, 1000, S)
    results = _nights(db, sub, variant, S, DELIST + 2)
    for i in range(S, DELIST):
        assert results[i].new_sell_signals == [], i
    result = results[DELIST]
    assert [s.exit_reason for s in result.new_sell_signals] == ["delisted"]
    # 次晚不重复
    assert results[DELIST + 1].new_sell_signals == []
    assert results[DELIST + 2].new_sell_signals == []


# ---------- 回溯录入、作废、幂等、通知、隔离 ----------


def test_backdated_trade_rewrites_derived_rows_but_not_signals(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain, {"risk.max_positions": 2})
    _nights(db, sub, plain, S, S + 4)
    book = _manual_book(db, sub)
    signals_before = [_columns(s) for s in _signals(db, book)]
    assert signals_before
    days_before = {
        r.day: (r.equity, r.cash, r.unit_nav)
        for r in db.scalars(select(BookDay).where(BookDay.book_id == book.id))
    }
    # 录入 3 个交易日前的买入
    e = _trade(db, sub, plain, Side.BUY, "600003", S + 1, 10.0, 1000, today=S + 4)
    assert e.origin == "self"
    db.expire_all()
    days_after = {
        r.day: (r.equity, r.cash, r.unit_nav)
        for r in db.scalars(select(BookDay).where(BookDay.book_id == book.id))
    }
    assert sorted(days_after) == sorted(days_before) == plain.days[S : S + 5]
    assert days_after[plain.days[S]] == days_before[plain.days[S]]
    for i in range(S + 1, S + 5):
        d = plain.days[i]
        assert days_after[d][1] < days_before[d][1]  # 现金从那天起减少
        assert days_after[d][0] != days_before[d][0]
    assert [_columns(s) for s in _signals(db, book)] == signals_before


def test_void_entry_appends_void_and_rewrites_state(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    buy = _trade(db, sub, plain, Side.BUY, "600003", S, 10.0, 1000)
    _night(db, sub, plain, S)
    book = _manual_book(db, sub)
    [cash_before] = db.scalars(select(BookDay.cash).where(BookDay.book_id == book.id))
    v = void_entry(
        db, sub.user_id, sub.id, plain.market, plain.calendar, plain.cfg, plain.days[S], buy.id
    )
    db.commit()
    assert v.kind == "void" and v.voids_entry_id == buy.id
    db.expire_all()
    [cash_after] = db.scalars(select(BookDay.cash).where(BookDay.book_id == book.id))
    assert cash_before < CASH and cash_after == CASH
    entries = list(db.scalars(select(LedgerEntry).where(LedgerEntry.book_id == book.id)))
    assert len(entries) == 2  # 原记录保留
    with pytest.raises(ValidationError, match="已作废"):
        void_entry(
            db, sub.user_id, sub.id, plain.market, plain.calendar, plain.cfg, plain.days[S], buy.id
        )


def test_advance_manual_book_idempotent(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)
    first = _night(db, sub, plain, S)
    assert first.new_buy_signals and first.new_sell_signals
    counts = _row_counts(db)
    again = _night(db, sub, plain, S)
    assert (
        again.new_buy_signals,
        again.new_sell_signals,
        again.reminders,
        again.abandoned,
        again.risk_events,
    ) == ([], [], [], [], [])
    assert _row_counts(db) == counts


def test_rerun_after_rollback_reproduces_same_signals(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    _holding(db, sub, plain, "600003", 12.0, 1000, S)

    def run() -> list[tuple[Any, ...]]:
        m = plain.market
        r = advance_manual_book(db, sub, plain.cfg, m.panel, m, plain.calendar, plain.days[S], True)
        return [
            (s.side, s.symbol, s.shares, s.ref_price, s.status, s.exit_reason)
            for s in [*r.new_buy_signals, *r.new_sell_signals]
        ]

    first = run()
    db.rollback()
    assert run() == first and first


def test_not_notify_marks_signals_unnotified(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    result = _night(db, sub, plain, S, notify=False)
    assert result.new_buy_signals
    assert all(s.status == "unnotified" for s in result.new_buy_signals)


def test_other_user_cannot_record_or_void(db: Session, plain: World) -> None:
    sub = _subscribe(db, plain)
    entry = _trade(db, sub, plain, Side.BUY, "600003", S, 10.0, 1000)
    other_sub = _subscribe(db, plain, email="other@example.com")
    other_id = other_sub.user_id
    m, cal, cfg, today = plain.market, plain.calendar, plain.cfg, plain.days[S]
    with pytest.raises(NotFoundError):
        record_trade(db, other_id, sub.id, m, cal, cfg, today, Side.BUY, "600001", today, 10, 100)
    with pytest.raises(NotFoundError):
        void_entry(db, other_id, sub.id, m, cal, cfg, today, entry.id)
    # 用自己的订阅也不能作废别人的流水
    with pytest.raises(NotFoundError):
        void_entry(db, other_id, other_sub.id, m, cal, cfg, today, entry.id)
    with pytest.raises(NotFoundError):
        record_cash(db, other_id, sub.id, m, cal, cfg, today, "deposit", today, 100.0)
    with pytest.raises(NotFoundError):
        manual_state(db, other_id, sub.id, m, cal, cfg, today)
