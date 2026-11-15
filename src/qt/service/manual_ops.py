"""手动账的录入与每晚推进（落库部分）。

录入：成交（买入、卖出）、已有持仓（转入）、入金出金、作废。流水只追加，更正靠追加一条作废记录。
每次录入先校验（不合法时抛 ValidationError，不写任何行），再写流水、关联信号，最后按流水重放整本账，
重写派生表 book_days 与 trades（先删后写），risk_events 只补插不存在的。

每晚推进：按流水重放到当日 → 卖出信号（新建、到期再提醒一次、条件不再满足时作废）→ 买入信号到期放弃
→ 买入信号（按手动账自己的现金与持仓定量）→ 暂停买入时取消未结的买入信号 → 写派生表。
信号是按使用者设定的规则计算出的提示，系统从不下单。

派生表只反映到 books.processed_through（最近一次处理过的交易日收盘）为止的状态。
本模块只 flush、不 commit，事务边界由调用方决定。
"""

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, cast

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.costs import CostModel
from qt.db import repo
from qt.db.models import Book, BookDay, LedgerEntry, Signal, Subscription
from qt.db.models import RiskEvent as RiskEventRow
from qt.db.models import Trade as TradeRow
from qt.features import FeaturePanel
from qt.models import BuyCandidate, ExitReason, Order, Side
from qt.risk import RiskManager
from qt.service.accounts import STRATEGY_NAME, subscription_config
from qt.service.errors import LedgerInconsistentError, ValidationError
from qt.service.manual_book import (
    Entry,
    EntryKind,
    LedgerReplayError,
    ManualState,
    Market,
    replay,
    validate_entry,
)
from qt.service.signals import (
    SignalStatus,
    create_signal,
    mark_signal,
    metrics_json,
    record_rejection,
)
from qt.strategy.high_dividend_low import HighDividendLow

# 未结的信号：录入成交时可以关联，每晚参与占用、到期、提醒与作废
_OPEN = (SignalStatus.CREATED.value, SignalStatus.NOTIFIED.value)
_VOID_NO_LONGER = "条件不再满足"
_VOID_PAUSED = "暂停买入时取消"
_EPS = 1e-6


class EntryMarket(Market, Protocol):
    """录入时需要的行情：在重放所需之外，还要知道有哪些股票、当日最高最低价。"""

    def known_symbols(self) -> AbstractSet[str]:
        """可以录入的股票代码。"""
        ...

    def price_range(self, day: date, symbol: str) -> tuple[float, float] | None:
        """当日 (最低价, 最高价)；当日没有日线返回 None。"""
        ...


@dataclass(frozen=True)
class ManualDayResult:
    """一晚推进的结果，供每日任务决定发什么邮件。"""

    new_buy_signals: list[Signal]
    new_sell_signals: list[Signal]
    reminders: list[Signal]  # 需要再提醒一次的卖出信号
    abandoned: list[Signal]  # 当晚到期被放弃的买入信号
    risk_events: list[RiskEventRow]  # 当晚新出现的
    state: ManualState


@dataclass(frozen=True)
class _Book:
    """一次操作用到的订阅、手动账、合并后的参数与成本模型。"""

    sub: Subscription
    book: Book
    cfg: AppConfig
    costs: CostModel


def _open_book(s: Session, sub: Subscription, cfg: AppConfig) -> _Book:
    book = repo.get_book_of_kind(s, sub.user_id, sub.id, "manual")
    merged = subscription_config(cfg, sub)
    return _Book(sub, book, merged, CostModel(merged.costs))


def _writable_book(s: Session, user_id: int, subscription_id: int, cfg: AppConfig) -> _Book:
    """录入用：订阅必须属于该使用者（否则 NotFoundError），且未关闭。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    if sub.status == "closed":
        raise ValidationError("订阅已关闭，手动账只能查看，不能再录入")
    return _open_book(s, sub, cfg)


# ---------- 由数据库行构建流水 ----------


def _metrics(raw: Mapping[str, Any]) -> dict[str, float]:
    """信号的指标：JSON 中的 null（原为 NaN 或无穷大）还原为 NaN。"""
    return {k: math.nan if v is None else float(v) for k, v in raw.items()}


def _entry_of(row: LedgerEntry, signal: Signal | None, market: Market) -> Entry:
    """数据库行转成重放用的流水：行业取自行情，买入指标与卖出原因取自关联信号。"""
    exit_reason: ExitReason | None = None
    metrics: dict[str, float] = {}
    if signal is not None and signal.side == Side.SELL.value and signal.exit_reason is not None:
        exit_reason = ExitReason(signal.exit_reason)
    if signal is not None and signal.side == Side.BUY.value:
        metrics = _metrics(signal.metrics)
    return Entry(
        id=row.id,
        kind=EntryKind(row.kind),
        trade_day=row.trade_day,
        effective_day=row.effective_day,
        symbol=row.symbol,
        shares=row.shares,
        price=row.price,
        fees=row.fees,
        origin=row.origin,
        exit_reason=exit_reason,
        voids_entry_id=row.voids_entry_id,
        industry="" if row.symbol is None else market.industry(row.symbol),
        entry_metrics=metrics,
    )


@dataclass(frozen=True)
class _Ledger:
    """账本的全部流水（数据库行与重放用的流水一一对应）及其关联信号。"""

    rows: list[LedgerEntry]
    entries: list[Entry]
    signals: dict[int, Signal]


def _load_ledger(s: Session, b: _Book, market: Market) -> _Ledger:
    rows = repo.list_ledger_entries(s, b.book.user_id, b.book.id)
    signals = {sig.id: sig for sig in repo.list_signals(s, b.book.user_id, b.book.id)}
    entries = [
        _entry_of(r, None if r.signal_id is None else signals.get(r.signal_id), market)
        for r in rows
    ]
    return _Ledger(rows, entries, signals)


def _replay(
    b: _Book, entries: Sequence[Entry], market: Market, calendar: TradingCalendar, through: date
) -> ManualState:
    """重放；已存流水不一致属于系统问题，抛 LedgerInconsistentError。"""
    try:
        return replay(
            entries,
            market,
            calendar,
            b.cfg.risk,
            b.costs,
            b.sub.initial_cash,
            b.sub.start_day,
            through,
        )
    except LedgerReplayError as exc:
        raise LedgerInconsistentError(f"账本现有流水已不一致，请联系管理员：{exc}") from exc


# ---------- 派生表 ----------


def _sell_entries_in_order(entries: Sequence[Entry], through: date) -> list[Entry]:
    """重放中实际应用的卖出流水，顺序与 ManualState.trades 一致（按日期，同日按录入顺序）。"""
    voided = {e.voids_entry_id for e in entries if e.kind is EntryKind.VOID}
    sells = [
        e
        for e in entries
        if e.kind is EntryKind.SELL and e.id not in voided and e.apply_day <= through
    ]
    return sorted(sells, key=lambda e: e.apply_day)  # sorted 是稳定排序，同日保持录入顺序


def _rewrite_derived(
    s: Session, b: _Book, ledger: _Ledger, state: ManualState, through: date
) -> list[RiskEventRow]:
    """重写 book_days 与 trades；risk_events 只插入不存在的，返回新插入的行。"""
    book = b.book
    s.execute(delete(BookDay).where(BookDay.book_id == book.id, BookDay.user_id == book.user_id))
    s.execute(delete(TradeRow).where(TradeRow.book_id == book.id, TradeRow.user_id == book.user_id))
    s.add_all(
        BookDay(
            book_id=book.id,
            user_id=book.user_id,
            day=day,
            equity=equity,
            cash=cash,
            unit_nav=nav,
        )
        for day, equity, cash, nav in zip(
            state.days, state.equity, state.cash_history, state.unit_nav, strict=True
        )
    )
    sells = _sell_entries_in_order(ledger.entries, through)
    if len(sells) != len(state.trades):
        raise LedgerInconsistentError(
            f"手动账有 {len(sells)} 笔卖出流水，重放却得到 {len(state.trades)} 笔交易"
        )
    row_of = {r.id: r for r in ledger.rows}
    for entry, trade in zip(sells, state.trades, strict=True):
        if entry.symbol != trade.symbol:
            raise LedgerInconsistentError(f"手动账卖出流水 {entry.id} 与重放出的交易对不上")
        signal_id = row_of[entry.id].signal_id
        signal = None if signal_id is None else ledger.signals.get(signal_id)
        s.add(
            TradeRow(
                book_id=book.id,
                user_id=book.user_id,
                symbol=trade.symbol,
                entry_day=trade.entry_day,
                exit_day=trade.exit_day,
                shares=trade.shares,
                cost=trade.cost,
                proceeds=trade.proceeds,
                cash_received=trade.cash_received,
                exit_reason="manual" if trade.exit_reason is None else trade.exit_reason.value,
                holding_days=trade.holding_days,
                entry_metrics=metrics_json(trade.entry_metrics),
                origin=trade.origin,
                signal_ref_price=None if signal is None else signal.ref_price,
                fill_price=trade.fill_price,
            )
        )
    existing = {(e.day, e.kind) for e in repo.list_risk_events(s, book.user_id, book.id)}
    added: list[RiskEventRow] = []
    for event in state.risk_events:
        if (event.day, event.kind.value) in existing:
            continue
        row = RiskEventRow(
            book_id=book.id,
            user_id=book.user_id,
            day=event.day,
            kind=event.kind.value,
            drawdown=event.drawdown,
            message=event.message,
        )
        s.add(row)
        added.append(row)
    s.flush()
    return added


def _refresh_after_entry(s: Session, b: _Book, market: Market, calendar: TradingCalendar) -> None:
    """录入后按全部流水重放到最近处理过的交易日，重写派生表；账本还没推进过则什么也不做。"""
    through = b.book.processed_through
    if through is None:
        return
    ledger = _load_ledger(s, b, market)
    state = _replay(b, ledger.entries, market, calendar, through)
    _rewrite_derived(s, b, ledger, state, through)


# ---------- 录入 ----------


def _validate(
    b: _Book,
    ledger: _Ledger,
    new: Entry,
    market: EntryMarket,
    calendar: TradingCalendar,
    today: date,
) -> None:
    validate_entry(
        ledger.entries,
        new,
        market,
        calendar,
        b.cfg.risk,
        b.costs,
        b.sub.initial_cash,
        b.sub.start_day,
        today=today,
        backdate_limit_days=b.cfg.service.backdate_limit_days,
        known_symbols=market.known_symbols(),
    )


def _next_id(ledger: _Ledger) -> int:
    """校验用的临时编号：大于现有全部流水，保证排在最后。"""
    return max((r.id for r in ledger.rows), default=0) + 1


def _estimate_fees(b: _Book, side: Side, price: float, shares: float, day: date) -> float:
    """按全站费用规则估算（佣金、过户费，卖出另加当日印花税）；使用者填的是实际成交价，不加滑点。"""
    exact = CostModel(b.cfg.costs.model_copy(update={"slippage": 0.0}))
    gross = price * shares
    if side is Side.BUY:
        return exact.buy_cost(price, shares) - gross
    return gross - exact.sell_proceeds(price, shares, day)


def _in_window(sig: Signal, side: Side, trade_day: date) -> bool:
    """成交日落在信号的有效窗口内：信号日 < 成交日；买入信号还要求成交日 <= 有效期。"""
    if not sig.signal_day < trade_day:
        return False
    if side is Side.BUY:
        return sig.valid_until is not None and trade_day <= sig.valid_until
    return True


def _rerecord_allowed(
    sig: Signal, ledger: _Ledger, calendar: TradingCalendar, valid_days: int, trade_day: date
) -> bool:
    """已执行的信号能否由更正后重新录入的成交再次关联。

    该信号关联的流水须全部已作废，且成交日不晚于"其中最晚成交日 + 有效天数"，
    防止很久以后的自主卖出被关联到一个早已作废的旧信号。
    """
    voided = {e.voids_entry_id for e in ledger.entries if e.kind is EntryKind.VOID}
    linked = [r for r in ledger.rows if r.kind != "void" and r.signal_id == sig.id]
    if not linked or any(r.id not in voided for r in linked):
        return False
    latest = max(r.trade_day for r in linked)
    try:
        bound = calendar.offset(latest, valid_days)
    except ValueError:
        return True  # 日历还没覆盖到上限日，成交日必然在上限之内
    return trade_day <= bound


def _find_link(
    s: Session,
    b: _Book,
    ledger: _Ledger,
    calendar: TradingCalendar,
    side: Side,
    symbol: str,
    trade_day: date,
) -> tuple[Signal | None, bool]:
    """录入成交时要关联的信号，返回 (信号, 是否要置为已执行)。

    规则见 record_trade；多个符合时取信号日最近的。
    """
    signals = [
        sig
        for sig in repo.list_signals(s, b.book.user_id, b.book.id, side.value)
        if sig.symbol == symbol and _in_window(sig, side, trade_day)
    ]
    open_ = [sig for sig in signals if sig.status in _OPEN]
    if open_:
        return max(open_, key=lambda sig: sig.signal_day), True
    valid_days = b.cfg.service.signal_valid_days

    def late(sig: Signal) -> bool:
        if sig.status == SignalStatus.EXECUTED.value:  # 更正后重新录入
            return _rerecord_allowed(sig, ledger, calendar, valid_days, trade_day)
        if side is Side.SELL:
            return (
                sig.status == SignalStatus.VOIDED.value
                and sig.void_note == _VOID_NO_LONGER
                and trade_day <= sig.status_day
            )
        return sig.status == SignalStatus.ABANDONED.value

    late_ = [sig for sig in signals if late(sig)]
    if late_:
        return max(late_, key=lambda sig: sig.signal_day), False
    return None, False


def _trade_notes(
    b: _Book,
    ledger: _Ledger,
    market: EntryMarket,
    calendar: TradingCalendar,
    side: Side,
    symbol: str,
    trade_day: date,
    price: float,
    shares: float,
) -> str:
    """不拒绝、只提示的情形：价格在当日最高最低价之外（或当日没有行情）、卖出违反 T+1。"""
    notes: list[str] = []
    span = market.price_range(trade_day, symbol)
    if span is None:
        notes.append(f"{symbol} 在 {trade_day} 没有行情（可能停牌），请核对日期")
    elif not span[0] - _EPS <= price <= span[1] + _EPS:
        notes.append(
            f"成交价 {price:.2f} 不在 {trade_day} 的最低价 {span[0]:.2f} 与最高价 {span[1]:.2f} "
            "之间，请核对"
        )
    if side is Side.SELL:
        # 可卖股数 = 当日日终持股（不含本笔）减去当日买入的股数；转入的持仓本来就持有，可以卖
        voided = {e.voids_entry_id for e in ledger.entries if e.kind is EntryKind.VOID}
        bought = sum(
            e.shares
            for e in ledger.entries
            if e.kind is EntryKind.BUY
            and e.id not in voided
            and e.symbol == symbol
            and e.trade_day == trade_day
        )
        if bought > 0:
            pos = _replay(b, ledger.entries, market, calendar, trade_day).positions.get(symbol)
            sellable = (0.0 if pos is None else pos.shares) - bought
            if shares > sellable + _EPS:
                notes.append(
                    f"卖出 {shares:g} 股超过可卖的 {max(sellable, 0.0):g} 股"
                    f"（当日买入 {bought:g} 股）；"
                    "A 股当日买入的股票次日才能卖出（T+1），请核对日期"
                )
    return "；".join(notes)


def record_trade(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: EntryMarket,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    side: Side,
    symbol: str,
    trade_day: date,
    price: float,
    shares: float,
    fees: float | None = None,
) -> LedgerEntry:
    """录入一笔买入或卖出成交。费用不填时按全站费用规则估算并标为估算值。

    自动关联同一本账里同一股票、方向相同、成交日在窗口内的信号
    （信号日 < 成交日，买入另需成交日 <= 有效期）：
    1. 未结（created/notified）的信号：关联并置为已执行；
    2. 没有时，不改状态地关联：已执行但关联流水全部作废、且成交日不晚于
       "被作废流水的最晚成交日 + 有效天数"的（作废后重新录入）、
       卖出信号因"条件不再满足"作废且成交日 <= 作废日的、买入信号已放弃的（晚录入）。
    多个符合时取信号日最近的。关联上流水记为 origin=signal，否则为 self；
    卖出原因与买入指标随之带入。
    价格超出当日最高最低价或卖出违反 T+1 时只在 note 里提示。
    """
    b = _writable_book(s, user_id, subscription_id, cfg)
    ledger = _load_ledger(s, b, market)
    estimated = fees is None
    fee = _estimate_fees(b, side, price, shares, trade_day) if fees is None else fees
    signal, execute = _find_link(s, b, ledger, calendar, side, symbol, trade_day)
    exit_reason: ExitReason | None = None
    metrics: dict[str, float] = {}
    if signal is not None and side is Side.SELL and signal.exit_reason is not None:
        exit_reason = ExitReason(signal.exit_reason)
    if signal is not None and side is Side.BUY:
        metrics = _metrics(signal.metrics)
    new = Entry(
        id=_next_id(ledger),
        kind=EntryKind(side.value),
        trade_day=trade_day,
        effective_day=trade_day,
        symbol=symbol,
        shares=shares,
        price=price,
        fees=fee,
        origin="self" if signal is None else "signal",
        exit_reason=exit_reason,
        voids_entry_id=None,
        industry=market.industry(symbol),
        entry_metrics=metrics,
    )
    _validate(b, ledger, new, market, calendar, today)
    note = _trade_notes(b, ledger, market, calendar, side, symbol, trade_day, price, shares)
    row = LedgerEntry(
        book_id=b.book.id,
        user_id=user_id,
        kind=side.value,
        symbol=symbol,
        trade_day=trade_day,
        effective_day=trade_day,
        shares=shares,
        price=price,
        fees=fee,
        fees_estimated=estimated,
        signal_id=None if signal is None else signal.id,
        origin=new.origin,
        created_by_user_id=user_id,
        note=note,
    )
    s.add(row)
    s.flush()
    if signal is not None and execute:
        mark_signal(s, signal, SignalStatus.EXECUTED, trade_day)
    _refresh_after_entry(s, b, market, calendar)
    repo.add_audit(
        s,
        user_id,
        "manual.record_trade",
        f"ledger_entry:{row.id}",
        {
            "side": side.value,
            "symbol": symbol,
            "trade_day": trade_day.isoformat(),
            "price": price,
            "shares": shares,
            "fees": fee,
            "fees_estimated": estimated,
            "signal_id": row.signal_id,
        },
    )
    return row


def record_holding(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: EntryMarket,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    symbol: str,
    buy_day: date,
    cost_price: float,
    shares: float,
) -> LedgerEntry:
    """录入一笔已有持仓（转入，标记为自主买入）：不扣现金，从今天起（含）第一个交易日进入净值。"""
    b = _writable_book(s, user_id, subscription_id, cfg)
    ledger = _load_ledger(s, b, market)
    upcoming = calendar.between(today, date.max)
    if not upcoming:
        raise ValidationError(f"交易日历还没有覆盖到 {today}，暂时不能录入已有持仓")
    effective = upcoming[0]
    new = Entry(
        id=_next_id(ledger),
        kind=EntryKind.TRANSFER_IN,
        trade_day=buy_day,
        effective_day=effective,
        symbol=symbol,
        shares=shares,
        price=cost_price,
        fees=0.0,
        origin="self",
        exit_reason=None,
        voids_entry_id=None,
        industry=market.industry(symbol),
        entry_metrics={},
    )
    _validate(b, ledger, new, market, calendar, today)
    row = LedgerEntry(
        book_id=b.book.id,
        user_id=user_id,
        kind=EntryKind.TRANSFER_IN.value,
        symbol=symbol,
        trade_day=buy_day,
        effective_day=effective,
        shares=shares,
        price=cost_price,
        fees=0.0,
        fees_estimated=False,
        cost_price=cost_price,
        origin="self",
        created_by_user_id=user_id,
        note="",
    )
    s.add(row)
    s.flush()
    _refresh_after_entry(s, b, market, calendar)
    repo.add_audit(
        s,
        user_id,
        "manual.record_holding",
        f"ledger_entry:{row.id}",
        {
            "symbol": symbol,
            "buy_day": buy_day.isoformat(),
            "effective_day": effective.isoformat(),
            "cost_price": cost_price,
            "shares": shares,
        },
    )
    return row


def record_cash(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: EntryMarket,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    kind: Literal["deposit", "withdraw"],
    day: date,
    amount: float,
) -> LedgerEntry:
    """录入入金或出金；金额记在流水的 price 上。"""
    b = _writable_book(s, user_id, subscription_id, cfg)
    ledger = _load_ledger(s, b, market)
    entry_kind = EntryKind(kind)
    new = Entry(
        id=_next_id(ledger),
        kind=entry_kind,
        trade_day=day,
        effective_day=day,
        symbol=None,
        shares=0.0,
        price=amount,
        fees=0.0,
        origin=None,
        exit_reason=None,
        voids_entry_id=None,
        industry="",
        entry_metrics={},
    )
    _validate(b, ledger, new, market, calendar, today)
    row = LedgerEntry(
        book_id=b.book.id,
        user_id=user_id,
        kind=entry_kind.value,
        symbol=None,
        trade_day=day,
        effective_day=day,
        shares=0.0,
        price=amount,
        fees=0.0,
        fees_estimated=False,
        created_by_user_id=user_id,
        note="",
    )
    s.add(row)
    s.flush()
    _refresh_after_entry(s, b, market, calendar)
    repo.add_audit(
        s,
        user_id,
        f"manual.{kind}",
        f"ledger_entry:{row.id}",
        {"day": day.isoformat(), "amount": amount},
    )
    return row


def void_entry(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: EntryMarket,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    entry_id: int,
) -> LedgerEntry:
    """作废一条流水：追加一条作废记录，原记录保留。关联信号的状态不回退（已执行是终态）。"""
    b = _writable_book(s, user_id, subscription_id, cfg)
    target = repo.get_ledger_entry(s, user_id, b.book.id, entry_id)
    ledger = _load_ledger(s, b, market)
    new = Entry(
        id=_next_id(ledger),
        kind=EntryKind.VOID,
        trade_day=today,
        effective_day=today,
        symbol=target.symbol,
        shares=0.0,
        price=0.0,
        fees=0.0,
        origin=None,
        exit_reason=None,
        voids_entry_id=target.id,
        industry="",
        entry_metrics={},
    )
    _validate(b, ledger, new, market, calendar, today)
    row = LedgerEntry(
        book_id=b.book.id,
        user_id=user_id,
        kind=EntryKind.VOID.value,
        symbol=target.symbol,
        trade_day=today,
        effective_day=today,
        shares=0.0,
        price=0.0,
        fees=0.0,
        fees_estimated=False,
        voids_entry_id=target.id,
        created_by_user_id=user_id,
        note="",
    )
    s.add(row)
    s.flush()
    _refresh_after_entry(s, b, market, calendar)
    repo.add_audit(
        s,
        user_id,
        "manual.void_entry",
        f"ledger_entry:{row.id}",
        {"voids_entry_id": target.id, "kind": target.kind},
    )
    return row


def manual_state(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: Market,
    calendar: TradingCalendar,
    cfg: AppConfig,
    through: date,
) -> ManualState:
    """按全部流水重放到 through（含）的手动账状态（只读）。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    b = _open_book(s, sub, cfg)
    return _replay(b, _load_ledger(s, b, market).entries, market, calendar, through)


# ---------- 每晚推进 ----------


def _check_day(b: _Book, calendar: TradingCalendar, day: date) -> None:
    """推进前的检查：订阅生效、交易日、首晚不早于起始日、之后逐日推进。"""
    sub, processed = b.sub, b.book.processed_through
    if sub.status != "active":
        raise ValidationError(f"订阅当前状态为 {sub.status}，不推进手动账")
    if sub.strategy_name != STRATEGY_NAME:
        raise ValidationError(f"不支持的策略 {sub.strategy_name}")
    if not calendar.is_trading_day(day):
        raise ValidationError(f"{day} 不是交易日")
    if processed is None:
        if day < sub.start_day:
            raise ValidationError(f"手动账从 {sub.start_day} 起推进，不能处理更早的 {day}")
        return
    expected = calendar.offset(processed, 1)
    if day != expected:
        raise ValidationError(
            f"手动账已推进到 {processed}，须按交易日逐日推进，下一个应处理 {expected}，"
            f"不能直接处理 {day}"
        )


def _publish(s: Session, signal: Signal, day: date, notify: bool) -> Signal:
    """新信号的状态：要通知则为已通知，否则（补跑）为未通知。"""
    status = SignalStatus.NOTIFIED if notify else SignalStatus.UNNOTIFIED
    mark_signal(s, signal, status, day)
    return signal


def _valid_until(calendar: TradingCalendar, day: date, valid_days: int) -> date | None:
    """买入信号最后一个可执行的交易日；日历还没覆盖到时为 None。"""
    try:
        return calendar.offset(day, valid_days)
    except ValueError:
        return None


@dataclass
class _Night:
    """一晚推进中的共用数据。"""

    s: Session
    b: _Book
    market: Market
    calendar: TradingCalendar
    day: date
    notify: bool
    state: ManualState
    snapshot: pd.DataFrame
    strategy: HighDividendLow


def _sell_side(n: _Night) -> tuple[list[Signal], list[Signal]]:
    """卖出信号：新建、到期再提醒一次、条件不再满足时作废。返回 (新信号, 再提醒)。"""
    s, b, day = n.s, n.b, n.day
    positions = list(n.state.positions.values())
    triggered: dict[str, tuple[ExitReason, str]] = {}
    for sig in n.strategy.sell_signals(day, n.snapshot, positions):
        triggered[sig.symbol] = (sig.reason, sig.detail)
    suspended: set[str] = set()
    for pos in positions:
        if n.market.delisted_before(day, pos.symbol):
            triggered[pos.symbol] = (
                ExitReason.DELISTED,
                f"{pos.symbol} 已退市，按你设定的规则提示卖出；请按实际处理结果录入卖出",
            )
        elif pos.symbol not in n.snapshot.index:
            suspended.add(pos.symbol)  # 停牌：当日无法判断，已有的卖出信号保持原状
    open_sells = repo.list_signals(s, b.book.user_id, b.book.id, Side.SELL.value, _OPEN)
    open_by_symbol = {sig.symbol: sig for sig in open_sells}
    valid_days = b.cfg.service.signal_valid_days
    reminders: list[Signal] = []
    for sig in open_sells:
        if sig.symbol in triggered:
            elapsed = n.calendar.count_between(sig.signal_day, day)
            if elapsed >= valid_days and sig.reminded_day is None:
                sig.reminded_day = day
                reminders.append(sig)
        elif sig.symbol not in suspended:
            mark_signal(s, sig, SignalStatus.VOIDED, day, _VOID_NO_LONGER)
    new: list[Signal] = []
    for pos in positions:
        hit = triggered.get(pos.symbol)
        if hit is None or pos.symbol in open_by_symbol:
            continue
        reason, detail = hit
        sig = create_signal(
            s,
            b.book,
            Side.SELL,
            pos.symbol,
            day,
            pos.shares,
            n.market.last_close(day, pos.symbol),
            detail,
            reason,
            {},
            None,  # 卖出信号不过期，满有效天数后再提醒一次
        )
        new.append(_publish(s, sig, day, n.notify))
    s.flush()
    return new, reminders


def _abandon_expired(n: _Night) -> list[Signal]:
    """有效期已过仍未录入成交的买入信号 → 已放弃。"""
    abandoned: list[Signal] = []
    for sig in repo.list_signals(n.s, n.b.book.user_id, n.b.book.id, Side.BUY.value, _OPEN):
        if sig.valid_until is not None and sig.valid_until < n.day:
            mark_signal(n.s, sig, SignalStatus.ABANDONED, n.day)
            abandoned.append(sig)
    return abandoned


def _recently_abandoned(n: _Night) -> set[str]:
    """最近 abandon_resignal_days 个交易日内（含当晚）在本账被放弃过的股票。"""
    limit = n.b.cfg.service.abandon_resignal_days
    rows = repo.list_signals(
        n.s, n.b.book.user_id, n.b.book.id, Side.BUY.value, (SignalStatus.ABANDONED.value,)
    )
    return {sig.symbol for sig in rows if n.calendar.count_between(sig.status_day, n.day) <= limit}


def _candidate_close(snapshot: pd.DataFrame, candidate: BuyCandidate) -> float:
    """候选的定仓价：当日快照收盘价，快照里没有则用候选自带的收盘价（与回测引擎一致）。"""
    if candidate.symbol in snapshot.index:
        return float(cast(float, snapshot.at[candidate.symbol, "close"]))
    return candidate.close


def _buy_side(n: _Night, risk: RiskManager) -> list[Signal]:
    """买入信号：持仓与未结买入信号的股票都算已持有，未结买入信号占用现金与持仓名额。"""
    s, b, day, state = n.s, n.b, n.day, n.state
    pending = repo.list_signals(s, b.book.user_id, b.book.id, Side.BUY.value, _OPEN)
    held = frozenset(state.positions) | frozenset(sig.symbol for sig in pending)
    blocked = _recently_abandoned(n)
    candidates = [
        c for c in n.strategy.buy_candidates(day, n.snapshot, held) if c.symbol not in blocked
    ]
    closes: dict[str, float] = {c.symbol: _candidate_close(n.snapshot, c) for c in candidates}
    for symbol in [*state.positions, *(sig.symbol for sig in pending)]:
        closes[symbol] = n.market.last_close(day, symbol)
    orders, rejections = risk.size_buys(
        day,
        candidates,
        state.equity[-1],
        state.cash,
        list(state.positions.values()),
        closes,
        [Order(sig.symbol, Side.BUY, sig.shares, sig.signal_day, sig.reason) for sig in pending],
    )
    gap = b.cfg.service.rejection_episode_gap_days
    for rejection in rejections:
        record_rejection(s, b.book, rejection, day, n.calendar, gap)
    by_symbol = {c.symbol: c for c in candidates}
    strategy_cfg = b.cfg.strategy
    valid_days = b.cfg.service.signal_valid_days
    new: list[Signal] = []
    for order in orders:
        ref = closes[order.symbol]
        sig = create_signal(
            s,
            b.book,
            Side.BUY,
            order.symbol,
            day,
            order.shares,
            ref,
            order.reason,
            None,
            by_symbol[order.symbol].metrics,
            _valid_until(n.calendar, day, valid_days),
        )
        sig.stop_price = ref * (1 - strategy_cfg.stop_loss)
        if strategy_cfg.take_profit_mode == "fixed":
            sig.take_price = ref * (1 + strategy_cfg.take_profit_fixed)
        new.append(_publish(s, sig, day, n.notify))
    s.flush()
    return new


def _void_open_buys_when_paused(n: _Night) -> None:
    """暂停买入期间不保留未结的买入信号（当晚触发暂停，或回溯录入使之前已触发暂停）。"""
    for sig in repo.list_signals(n.s, n.b.book.user_id, n.b.book.id, Side.BUY.value, _OPEN):
        mark_signal(n.s, sig, SignalStatus.VOIDED, n.day, _VOID_PAUSED)


def advance_manual_book(
    s: Session,
    sub: Subscription,
    cfg: AppConfig,
    panel: FeaturePanel,
    market: Market,
    calendar: TradingCalendar,
    day: date,
    notify: bool,
) -> ManualDayResult:
    """把订阅的手动账推进到交易日 day：按流水重放、产生与更新信号、写派生表。

    已处理过（processed_through >= day）时不写任何东西，返回空结果（state 为重放到 day 的状态）。
    首晚不早于订阅起始日，之后必须按交易日逐日推进。notify 为假（补跑）时新信号记为未通知。
    """
    b = _open_book(s, sub, cfg)
    ledger = _load_ledger(s, b, market)
    processed = b.book.processed_through
    if processed is not None and processed >= day:
        state = _replay(b, ledger.entries, market, calendar, day)
        return ManualDayResult([], [], [], [], [], state)
    _check_day(b, calendar, day)
    state = _replay(b, ledger.entries, market, calendar, day)
    if not state.days or state.days[-1] != day:
        raise LedgerInconsistentError(f"手动账重放没有得到 {day} 的结果")
    risk = RiskManager(b.cfg.risk, calendar, b.costs)
    risk.load_state(state.risk)
    night = _Night(
        s,
        b,
        market,
        calendar,
        day,
        notify,
        state,
        panel.snapshot(day),
        HighDividendLow(b.cfg.strategy, b.cfg.universe),
    )
    new_sells, reminders = _sell_side(night)
    abandoned = _abandon_expired(night)
    new_buys: list[Signal] = []
    if risk.buying_allowed():
        new_buys = _buy_side(night, risk)
    else:
        _void_open_buys_when_paused(night)
    events = _rewrite_derived(s, b, ledger, state, day)
    b.book.processed_through = day
    s.flush()
    return ManualDayResult(new_buys, new_sells, reminders, abandoned, events, state)
