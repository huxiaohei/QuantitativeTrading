"""系统账逐日推进并落库：每晚读状态、推进一天、写回；结果必须与一次性回测逐项相同。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.backtest.engine import BacktestResult, limit_prices
from qt.backtest.report import clamp_period, run_period
from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import (
    Base,
    Book,
    BookDay,
    LedgerEntry,
    RiskEvent,
    Signal,
    Subscription,
    SystemState,
    Trade,
)
from qt.db.models import Rejection as RejectionRow
from qt.features import FeaturePanel, build_panel
from qt.models import BuyCandidate, ExitReason
from qt.risk import Rejection, RiskEventKind
from qt.service.accounts import (
    create_subscription,
    create_user,
    subscription_config,
    update_params,
)
from qt.service.errors import ValidationError
from qt.service.signals import SignalStatus, mark_signal, record_rejection
from qt.service.system_book import advance_system_book
from tests.fakes import (
    TRADING_SYMBOLS,
    make_ohlc_bars,
    make_securities,
    populate_trading_store,
)

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
CASH = CFG.backtest.initial_cash  # run_period 用站点本金，订阅本金取同一值才可比较
LIMIT_UP_DAYS = (250, 251, 252)  # 变体数据中 600004 开盘涨停的交易日序号（买入信号在第 249 天）
DELIST_INDEX = 380  # 变体数据中 600003 的退市日序号（持仓期间退市）
SIGNAL_DAY = 249  # 两份数据集中首批买入信号的交易日序号
TINY_PAUSE: dict[str, object] = {"risk.pause_drawdown": 0.0003, "risk.warn_drawdown": 0.0001}


@dataclass(frozen=True)
class Market:
    """一份数据集：存储、日历、站点参数下的指标面板与全部交易日。"""

    store: ParquetStore
    calendar: TradingCalendar
    panel: FeaturePanel
    days: list[date]


def _market(store: ParquetStore, days: Sequence[date]) -> Market:
    calendar, start, end = clamp_period(store, days[0], days[-1])
    panel = build_panel(store, calendar, CFG, start, end)
    return Market(store, calendar, panel, calendar.between(start, end))


def _make_variant(store: ParquetStore) -> list[date]:
    """在交易数据集上加两处变化：600004 在买入信号后连续三天开盘涨停（买单过期或被暂停取消），
    600003 在持仓期间退市（退市清算）。"""
    days = populate_trading_store(store)
    bars = store.read(Table.BARS, symbols=["600004"])
    closes = [float(c) for c in bars["close"]]
    opens = list(closes)
    for i in LIMIT_UP_DAYS:
        opens[i] = limit_prices(closes[i - 1], False)[0]
    frame = make_ohlc_bars("600004", days, opens, closes)
    frame["high"] = pd.Series([max(o, c) for o, c in zip(opens, closes, strict=True)])
    frame["amount"] = pd.Series([1e8] * len(days), dtype="float64")
    store.write(Table.BARS, frame, symbol="600004")
    delist_day = days[DELIST_INDEX]
    bars3 = store.read(Table.BARS, symbols=["600003"])
    kept = bars3[bars3["day"] < pd.Timestamp(delist_day)].reset_index(drop=True)
    store.write(Table.BARS, kept, symbol="600003")
    rows = [
        (s, "sh_main", date(2010, 1, 1), delist_day if s == "600003" else None)
        for s in TRADING_SYMBOLS
    ]
    store.write(Table.SECURITIES, make_securities(rows))
    return days


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Market:
    store = ParquetStore(tmp_path_factory.mktemp("plain"))
    return _market(store, populate_trading_store(store))


@pytest.fixture(scope="module")
def variant(tmp_path_factory: pytest.TempPathFactory) -> Market:
    store = ParquetStore(tmp_path_factory.mktemp("variant"))
    return _market(store, _make_variant(store))


def _subscribe(
    db: Session, start: date, overrides: dict[str, object] | None = None
) -> Subscription:
    user = create_user(db, "sys@example.com")
    return create_subscription(db, user.id, CFG, CASH, start, overrides)


def _system_book(db: Session, sub: Subscription) -> Book:
    [book] = [b for b in repo.list_books(db, sub.user_id, sub.id) if b.kind == "system"]
    return book


def _advance(db: Session, sub: Subscription, market: Market, days: Sequence[date]) -> None:
    for day in days:
        advance_system_book(db, sub, CFG, market.panel, market.calendar, day)
        db.commit()
        db.expire_all()  # 下一晚一切从数据库重新读取


def _signals(db: Session, book: Book) -> list[Signal]:
    stmt = select(Signal).where(Signal.book_id == book.id).order_by(Signal.id)
    return list(db.scalars(stmt))


def _rejections(db: Session, book: Book) -> list[RejectionRow]:
    stmt = select(RejectionRow).where(RejectionRow.book_id == book.id).order_by(RejectionRow.id)
    return list(db.scalars(stmt))


def _expected_episodes(
    result: BacktestResult, calendar: TradingCalendar, gap: int
) -> list[tuple[str, str, date, date]]:
    """按"连续出现算一次"把回测的拒绝记录归并成事件 (股票, 规则, 首日, 末日)。"""
    episodes: list[tuple[str, str, date, date]] = []
    latest: dict[tuple[str, str], int] = {}  # (股票, 规则) -> episodes 中的下标
    for r in result.rejections:
        key = (r.candidate.symbol, r.rule)
        day = r.candidate.day
        i = latest.get(key)
        if i is not None and calendar.count_between(episodes[i][3], day) <= gap:
            episodes[i] = (*episodes[i][:3], day)
            continue
        latest[key] = len(episodes)
        episodes.append((key[0], key[1], day, day))
    return episodes


CASES: list[tuple[str, dict[str, object]]] = [
    ("plain", {}),
    # 两次暂停、两次恢复
    ("plain", {"risk.pause_drawdown": 0.02, "risk.warn_drawdown": 0.01}),
    # 600004 涨停三天买单超过有效期；600003 持仓期间退市
    ("variant", {}),
    # 首批成交当天的费用就触发暂停，涨停买不进的 600004 买单被暂停取消
    ("variant", TINY_PAUSE),
]


@pytest.mark.parametrize(("market_name", "overrides"), CASES, ids=[str(c) for c in CASES])
def test_daily_advance_through_database_equals_run_backtest(
    db: Session, request: pytest.FixtureRequest, market_name: str, overrides: dict[str, object]
) -> None:
    market: Market = request.getfixturevalue(market_name)
    start, end = market.days[0], market.days[-1]
    sub = _subscribe(db, start, overrides)
    db.commit()
    _advance(db, sub, market, market.days)

    whole = run_period(subscription_config(CFG, sub), market.store, start, end)
    book = _system_book(db, sub)
    assert book.processed_through == end

    # 每日净值：逐位相等
    days = list(db.scalars(select(BookDay).where(BookDay.book_id == book.id).order_by(BookDay.day)))
    assert [r.day for r in days] == list(whole.equity.index)
    assert [r.equity for r in days] == pytest.approx(list(whole.equity), rel=0, abs=0)
    assert [r.equity.hex() for r in days] == [v.hex() for v in whole.equity]
    assert [r.cash for r in days] == pytest.approx(list(whole.cash), rel=0, abs=0)
    assert [r.unit_nav for r in days] == [r.equity / CASH for r in days]
    assert {r.user_id for r in days} == {sub.user_id}

    # 完整交易
    trades = list(db.scalars(select(Trade).where(Trade.book_id == book.id).order_by(Trade.id)))
    assert [
        (t.symbol, t.entry_day, t.exit_day, t.cost, t.proceeds, t.exit_reason) for t in trades
    ] == [
        (t.symbol, t.entry_day, t.exit_day, t.cost, t.proceeds, t.exit_reason.value)
        for t in whole.trades
    ]
    assert [(t.holding_days, t.entry_metrics) for t in trades] == [
        (t.holding_days, dict(t.entry_metrics)) for t in whole.trades
    ]
    assert all(t.origin == "signal" and t.cash_received == 0.0 for t in trades)

    # 风控事件
    events = list(
        db.scalars(select(RiskEvent).where(RiskEvent.book_id == book.id).order_by(RiskEvent.id))
    )
    assert {(e.day, e.kind) for e in events} == {(e.day, e.kind.value) for e in whole.risk_events}
    assert [(e.day, e.kind, e.drawdown, e.message) for e in events] == [
        (e.day, e.kind.value, e.drawdown, e.message) for e in whole.risk_events
    ]

    # 期末状态：现金与持仓
    state_row = db.get(SystemState, book.id)
    assert state_row is not None and state_row.as_of_day == end
    final, _ = load_state(state_row.state)
    assert final.cash == whole.cash.iloc[-1]
    assert list(final.positions.values()) == whole.open_positions

    # 成交流水：每笔成交一条
    ledger = list(
        db.scalars(
            select(LedgerEntry).where(LedgerEntry.book_id == book.id).order_by(LedgerEntry.id)
        )
    )
    assert all(e.origin == "signal" and e.user_id == sub.user_id for e in ledger)
    sells = [e for e in ledger if e.kind == "sell"]
    assert [(e.symbol, e.trade_day, e.shares) for e in sells] == [
        (t.symbol, t.exit_day, t.shares) for t in trades
    ]
    assert [e.price for e in sells] == [t.fill_price for t in trades]
    buys = [e for e in ledger if e.kind == "buy"]
    bought = [*whole.trades, *whole.open_positions]
    assert sorted((e.trade_day, str(e.symbol)) for e in buys) == sorted(
        (x.entry_day, x.symbol) for x in bought
    )

    # 信号：成交的买入信号都已执行，作废的买单与 expired_buys 一致
    signals = _signals(db, book)
    executed_buys = sorted(
        (s.status_day, s.symbol) for s in signals if s.side == "buy" and s.status == "executed"
    )
    assert executed_buys == sorted((x.entry_day, x.symbol) for x in bought)
    assert all(e.signal_id is not None for e in buys)
    voided_buys = sorted(
        (s.symbol, s.signal_day, s.void_note or "")
        for s in signals
        if s.side == "buy" and s.status == "voided"
    )
    assert [(sym, d) for sym, d, _ in voided_buys] == sorted(
        (o.symbol, o.signal_day) for o in whole.expired_buys
    )
    executed_sells = [s for s in signals if s.side == "sell" and s.status == "executed"]
    not_delisted = [t for t in trades if t.exit_reason != ExitReason.DELISTED.value]
    assert sorted((s.status_day, s.symbol) for s in executed_sells) == sorted(
        (t.exit_day, t.symbol) for t in not_delisted
    )
    by_key = {(s.side, s.symbol, s.signal_day): s for s in signals}
    for t in trades:
        if t.exit_reason == ExitReason.DELISTED.value:
            assert t.signal_ref_price is None
    for t, e in zip(trades, sells, strict=True):
        if t.exit_reason != ExitReason.DELISTED.value:
            assert e.signal_id is not None
            sig = db.get(Signal, e.signal_id)
            assert sig is not None and sig.status == "executed"
            assert t.signal_ref_price == sig.ref_price
            assert by_key[("sell", sig.symbol, sig.signal_day)] is sig
            assert sig.ref_price == market.panel.last_close(sig.signal_day, sig.symbol)
    assert {s.user_id for s in signals} == {sub.user_id}
    assert all(s.status != "notified" for s in signals)

    # 拒绝记录按事件归并
    gap = CFG.service.rejection_episode_gap_days
    assert sorted(
        (r.symbol, r.rule, r.first_day, r.last_day) for r in _rejections(db, book)
    ) == sorted(_expected_episodes(whole, market.calendar, gap))

    # 防止测试退化：各组合确实覆盖了要测的路径
    reasons = {t.exit_reason for t in whole.trades}
    kinds = {e.kind for e in whole.risk_events}
    if (market_name, overrides) == CASES[0]:
        assert ExitReason.STOP_LOSS in reasons and ExitReason.TAKE_PROFIT_FIXED in reasons
        assert whole.open_positions and whole.rejections
    if (market_name, overrides) == CASES[1]:
        assert RiskEventKind.PAUSE in kinds and RiskEventKind.RESUME in kinds
    if (market_name, overrides) == CASES[2]:
        assert whole.expired_buys
        assert ExitReason.DELISTED in reasons
        assert voided_buys and all("超过有效期未成交" in n for _, _, n in voided_buys)
    if (market_name, overrides) == CASES[3]:
        assert whole.expired_buys and RiskEventKind.PAUSE in kinds
        assert voided_buys and all("暂停买入时取消" in n for _, _, n in voided_buys)


def _table_counts(db: Session) -> dict[str, int]:
    return {
        t.name: db.scalar(select(func.count()).select_from(t)) or 0
        for t in Base.metadata.sorted_tables
    }


def test_advance_is_idempotent_for_same_day(db: Session, plain: Market) -> None:
    days = plain.days[SIGNAL_DAY : SIGNAL_DAY + 2]
    sub = _subscribe(db, days[0])
    db.commit()
    _advance(db, sub, plain, days)
    counts = _table_counts(db)
    state = db.get(SystemState, _system_book(db, sub).id)
    assert state is not None
    snapshot = (state.as_of_day, state.state)
    assert counts["signals"] > 0 and counts["ledger_entries"] > 0
    for day in days:  # 当天与更早的交易日都不再处理
        assert advance_system_book(db, sub, CFG, plain.panel, plain.calendar, day) is None
    db.commit()
    db.expire_all()
    assert _table_counts(db) == counts
    state = db.get(SystemState, _system_book(db, sub).id)
    assert state is not None and (state.as_of_day, state.state) == snapshot


def test_advance_refuses_to_skip_trading_days(db: Session, plain: Market) -> None:
    sub = _subscribe(db, plain.days[SIGNAL_DAY])
    _advance(db, sub, plain, [plain.days[SIGNAL_DAY]])
    with pytest.raises(ValidationError, match="逐日"):
        advance_system_book(db, sub, CFG, plain.panel, plain.calendar, plain.days[SIGNAL_DAY + 2])


def test_param_change_takes_effect_from_next_advance_without_resetting_book(
    db: Session, plain: Market
) -> None:
    # 600002 在第 250 天买入后下跌：第 267 天收益落在 -15% 与 -10% 之间
    d_index = 266
    days = plain.days[240 : d_index + 1]
    sub = _subscribe(db, days[0])
    db.commit()
    _advance(db, sub, plain, days)
    book = _system_book(db, sub)
    assert not [s for s in _signals(db, book) if s.side == "sell"]
    before = db.get(SystemState, book.id)
    assert before is not None
    state_before, _ = load_state(before.state)
    assert set(state_before.positions) == set(TRADING_SYMBOLS)

    next_day = plain.days[d_index + 1]
    pos = state_before.positions["600002"]
    ret = pos.ret(plain.panel.last_close(next_day, "600002"))
    assert -0.15 < ret <= -0.10  # 默认止损线 15% 不触发，10% 触发

    update_params(db, sub.user_id, sub.id, CFG, {"strategy.stop_loss": 0.10}, next_day)
    db.commit()
    _advance(db, sub, plain, [next_day])

    after = db.get(SystemState, book.id)
    assert after is not None
    state_after, _ = load_state(after.state)
    assert state_after.cash == state_before.cash  # 账本没有重置
    assert {s: p.shares for s, p in state_after.positions.items()} == {
        s: p.shares for s, p in state_before.positions.items()
    }
    nav_days = db.scalars(select(BookDay.day).where(BookDay.book_id == book.id))
    assert len(list(nav_days)) == len(days) + 1
    sells = [s for s in _signals(db, book) if s.side == "sell"]
    assert [(s.symbol, s.signal_day, s.exit_reason, s.status) for s in sells] == [
        ("600002", next_day, "stop_loss", "created")
    ]
    assert [o.symbol for o in state_after.sell_orders] == ["600002"]


def test_signal_statuses_follow_fills_and_expiry(db: Session, variant: Market) -> None:
    days = variant.days
    sub = _subscribe(db, days[240])
    db.commit()
    _advance(db, sub, variant, days[240 : SIGNAL_DAY + 1])
    book = _system_book(db, sub)
    created = {s.symbol: s for s in _signals(db, book)}
    assert set(created) == set(TRADING_SYMBOLS)
    assert {(s.side, s.status, s.signal_day, s.status_day) for s in created.values()} == {
        ("buy", "created", days[SIGNAL_DAY], days[SIGNAL_DAY])
    }
    s1 = created["600001"]
    assert s1.ref_price == variant.panel.last_close(days[SIGNAL_DAY], "600001")
    assert s1.valid_until == days[SIGNAL_DAY + CFG.backtest.buy_order_valid_days]
    assert s1.metrics and s1.reason

    # 次日开盘成交 → 已执行；600004 涨停买不进，仍在有效期内
    _advance(db, sub, variant, [days[SIGNAL_DAY + 1]])
    statuses = {s.symbol: (s.status, s.status_day) for s in _signals(db, book)}
    for symbol in ("600001", "600002", "600003"):
        assert statuses[symbol] == ("executed", days[SIGNAL_DAY + 1])
    assert statuses["600004"] == ("created", days[SIGNAL_DAY])
    ledger = list(db.scalars(select(LedgerEntry).where(LedgerEntry.book_id == book.id)))
    linked = {s.symbol for s in _signals(db, book) if s.id in {e.signal_id for e in ledger}}
    assert linked == {"600001", "600002", "600003"}

    # 满 3 个交易日仍未成交 → 作废
    _advance(db, sub, variant, days[SIGNAL_DAY + 2 : SIGNAL_DAY + 4])
    [s4] = [s for s in _signals(db, book) if s.symbol == "600004" and s.signal_day == days[249]]
    assert (s4.status, s4.status_day) == ("voided", days[SIGNAL_DAY + 3])
    assert s4.void_note is not None and "超过有效期未成交" in s4.void_note


def test_repeated_rejection_is_one_episode(db: Session, plain: Market) -> None:
    days = plain.days[SIGNAL_DAY : SIGNAL_DAY + 5]
    sub = _subscribe(db, days[0], {"risk.max_positions": 3})
    db.commit()
    _advance(db, sub, plain, days)
    rows = [r for r in _rejections(db, _system_book(db, sub)) if r.rule == "max_positions"]
    assert len(rows) == 1
    [row] = rows
    assert (row.first_day, row.last_day) == (days[0], days[-1])
    assert plain.calendar.count_between(row.first_day, row.last_day) == 4
    assert row.user_id == sub.user_id and row.message and row.metrics


def _rejection(symbol: str, day: date) -> Rejection:
    candidate = BuyCandidate(symbol, day, 10.0, "银行", {"dividend_yield": 0.05}, "股息率 5.0%")
    return Rejection(candidate, "cooldown", "冷却期内")


def test_rejection_gap_longer_than_setting_starts_new_episode(db: Session, plain: Market) -> None:
    sub = _subscribe(db, plain.days[0])
    book = _system_book(db, sub)
    gap = CFG.service.rejection_episode_gap_days
    cal = plain.calendar
    d0 = plain.days[300]
    first = record_rejection(db, book, _rejection("600001", d0), d0, cal, gap)
    d1 = cal.offset(d0, gap)
    assert record_rejection(db, book, _rejection("600001", d1), d1, cal, gap) is first
    assert first.last_day == d1 and first.first_day == d0
    d2 = cal.offset(d1, gap + 1)
    second = record_rejection(db, book, _rejection("600001", d2), d2, cal, gap)
    assert second is not first and (second.first_day, second.last_day) == (d2, d2)
    # 其他股票或其他规则互不合并
    other = record_rejection(db, book, _rejection("600002", d2), d2, cal, gap)
    assert other is not second
    assert len(_rejections(db, book)) == 3


def test_mark_signal_refuses_to_change_a_finished_signal(db: Session, plain: Market) -> None:
    days = plain.days[SIGNAL_DAY : SIGNAL_DAY + 2]
    sub = _subscribe(db, days[0])
    db.commit()
    _advance(db, sub, plain, days)
    executed = next(s for s in _signals(db, _system_book(db, sub)) if s.status == "executed")
    with pytest.raises(ValidationError):
        mark_signal(db, executed, SignalStatus.VOIDED, days[1], "不应生效")
    assert executed.status == "executed"


def test_first_advance_starts_all_cash(db: Session, plain: Market) -> None:
    day = plain.days[100]
    sub = _subscribe(db, day)
    db.commit()
    log = advance_system_book(db, sub, CFG, plain.panel, plain.calendar, day)
    assert log is not None and log.equity == CASH and log.cash == CASH
    book = _system_book(db, sub)
    row = db.get(BookDay, (book.id, day))
    assert row is not None and (row.equity, row.cash, row.unit_nav) == (CASH, CASH, 1.0)
    assert book.processed_through == day


# ---------- 验收保险：退市日有挂单、买单因现金不足作废 ----------

SQUEEZE_DELIST = 407  # 600003 止损卖单挂着的那天退市
SQUEEZE: dict[str, object] = {"risk.max_position_pct": 0.33, "risk.max_industry_pct": 0.5}


def _make_squeezed(store: ParquetStore) -> list[date]:
    """首批四只买单按收盘价定量后几乎用尽现金，次日四只都高开 3%：最后一笔买不起一手而作废；
    600003 在止损卖单挂着的那天退市（挂单作废，按上一交易日收盘价清算）。"""
    days = populate_trading_store(store)
    for symbol in TRADING_SYMBOLS:
        bars = store.read(Table.BARS, symbols=[symbol])
        closes = [float(c) for c in bars["close"]]
        opens = list(closes)
        opens[SIGNAL_DAY + 1] = round(closes[SIGNAL_DAY] * 1.03, 2)
        frame = make_ohlc_bars(symbol, days, opens, closes)
        frame["high"] = pd.Series([max(o, c) for o, c in zip(opens, closes, strict=True)])
        frame["amount"] = pd.Series([1e8] * len(days), dtype="float64")
        if symbol == "600003":
            frame = frame[frame["day"] < pd.Timestamp(days[SQUEEZE_DELIST])].reset_index(drop=True)
        store.write(Table.BARS, frame, symbol=symbol)
    rows = [
        (s, "sh_main", date(2010, 1, 1), days[SQUEEZE_DELIST] if s == "600003" else None)
        for s in TRADING_SYMBOLS
    ]
    store.write(Table.SECURITIES, make_securities(rows))
    return days


def test_database_path_with_delisting_pending_order_and_no_cash_void_equals_backtest(
    db: Session, tmp_path: Path
) -> None:
    store = ParquetStore(tmp_path)
    market = _market(store, _make_squeezed(store))
    start, end = market.days[0], market.days[-1]
    sub = _subscribe(db, start, SQUEEZE)
    db.commit()
    _advance(db, sub, market, market.days)
    whole = run_period(subscription_config(CFG, sub), market.store, start, end)
    book = _system_book(db, sub)
    days = list(db.scalars(select(BookDay).where(BookDay.book_id == book.id).order_by(BookDay.day)))
    assert [r.day for r in days] == list(whole.equity.index)
    assert [r.equity for r in days] == pytest.approx(list(whole.equity), rel=0, abs=0)
    assert [r.equity.hex() for r in days] == [v.hex() for v in whole.equity]
    assert [r.cash for r in days] == pytest.approx(list(whole.cash), rel=0, abs=0)
    trades = list(db.scalars(select(Trade).where(Trade.book_id == book.id).order_by(Trade.id)))
    assert [(t.symbol, t.exit_day, t.cost, t.proceeds, t.exit_reason) for t in trades] == [
        (t.symbol, t.exit_day, t.cost, t.proceeds, t.exit_reason.value) for t in whole.trades
    ]
    state_row = db.get(SystemState, book.id)
    assert state_row is not None
    final, _ = load_state(state_row.state)
    assert final.cash == whole.cash.iloc[-1]
    assert list(final.positions.values()) == whole.open_positions
    # 两种作废都确实发生，备注与原因一致
    delist_day = market.days[SQUEEZE_DELIST]
    assert ("600003", delist_day, ExitReason.DELISTED) in {
        (t.symbol, t.exit_day, t.exit_reason) for t in whole.trades
    }
    voided = {(s.side, s.symbol, s.void_note) for s in _signals(db, book) if s.status == "voided"}
    assert ("sell", "600003", "股票退市，挂单取消") in voided
    assert ("buy", "600004", "现金不足，买不起一手，买单取消") in voided
    assert [(o.symbol, o.signal_day) for o in whole.expired_buys] == [
        ("600004", market.days[SIGNAL_DAY])
    ]
