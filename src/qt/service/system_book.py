"""系统账每晚推进一个交易日并落库。

每晚：从 system_states 读出引擎与风控状态 → 用回测引擎的 step_day 推进一天 → 把新状态和当天
发生的一切（成交、信号、被拒候选、风控事件、完整交易、每日净值）写回数据库。逐日推进的结果与
回测引擎一次跑完逐项相同。本模块只 flush、不 commit，事务边界由调用方（每日任务）决定。
"""

from datetime import date

from sqlalchemy.orm import Session

from qt.backtest.engine import BuyVoidReason, DayLog, EngineState, step_day
from qt.backtest.state import dump_state, load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.costs import CostModel
from qt.db import repo
from qt.db.models import Book, BookDay, LedgerEntry, Signal, Subscription, SystemState
from qt.db.models import RiskEvent as RiskEventRow
from qt.db.models import Trade as TradeRow
from qt.features import FeaturePanel
from qt.models import Fill, Side
from qt.risk import RiskManager
from qt.service.accounts import STRATEGY_NAME, subscription_config
from qt.service.errors import ValidationError
from qt.service.signals import (
    SignalStatus,
    create_signal,
    mark_signal,
    metrics_json,
    record_rejection,
)
from qt.strategy.high_dividend_low import HighDividendLow

_VOID_NOTES = {
    BuyVoidReason.EXPIRED: "超过有效期未成交",
    BuyVoidReason.NO_CASH: "现金不足，买不起一手，买单取消",
    BuyVoidReason.PAUSED: "暂停买入时取消",
}
_DELISTED_NOTE = "股票退市，挂单取消"
_DELISTING_FILL_NOTE = "退市清算，按上一交易日收盘价卖出"

OrderKey = tuple[Side, str, date]  # (方向, 股票, 信号日)，与 signals 表的自然键对应


def _valid_until(calendar: TradingCalendar, day: date, valid_days: int) -> date | None:
    """买入信号最后一个可成交的交易日；日历还没覆盖到时为 None。"""
    try:
        return calendar.offset(day, valid_days)
    except ValueError:
        return None


def _signal_of(s: Session, book: Book, key: OrderKey) -> Signal:
    side, symbol, signal_day = key
    signal = repo.find_signal(s, book.user_id, book.id, side.value, symbol, signal_day)
    if signal is None:
        raise ValueError(f"系统账缺少 {signal_day} {symbol} 的{side.value}信号，账本数据不一致")
    return signal


def _is_delisting(fill: Fill) -> bool:
    """退市清算没有信号，Fill.signal_day 记为清算当天；正常成交的信号日一定早于成交日。"""
    return fill.side is Side.SELL and fill.signal_day == fill.day


def _load(
    s: Session,
    sub: Subscription,
    book: Book,
    risk: RiskManager,
    calendar: TradingCalendar,
    day: date,
) -> tuple[EngineState, date | None]:
    """读出推进前的引擎状态（风控状态直接载入 risk），返回 (状态, 上一交易日)。"""
    if book.processed_through is None:
        if day < sub.start_day:
            raise ValidationError(f"系统账从 {sub.start_day} 起推进，不能处理更早的 {day}")
        empty = EngineState(
            cash=sub.initial_cash, positions={}, sell_orders=[], buy_orders=[], candidates={}
        )
        return empty, None
    prev_day = calendar.offset(day, -1)
    if prev_day != book.processed_through:
        raise ValidationError(
            f"系统账已推进到 {book.processed_through}，须按交易日逐日推进，"
            f"下一个应处理 {calendar.offset(book.processed_through, 1)}，不能直接处理 {day}"
        )
    row = repo.get_system_state(s, sub.user_id, book.id)
    if row is None:
        raise ValueError(f"系统账 {book.id} 已推进到 {prev_day}，却没有保存的状态")
    state, risk_state = load_state(row.state)
    risk.load_state(risk_state)
    return state, prev_day


def _save_state(s: Session, book: Book, state: EngineState, risk: RiskManager, day: date) -> None:
    raw = dump_state(state, risk.dump_state())
    row = repo.get_system_state(s, book.user_id, book.id)
    if row is None:
        s.add(SystemState(book_id=book.id, user_id=book.user_id, as_of_day=day, state=raw))
    else:
        row.as_of_day = day
        row.state = raw


def _record_fills(s: Session, book: Book, log: DayLog) -> None:
    """每笔成交一条流水（origin=signal），对应信号改为已执行；卖出成交同时写完整交易。"""
    sells = [f for f in log.fills if f.side is Side.SELL]
    if [f.symbol for f in sells] != [t.symbol for t in log.trades]:
        raise ValueError(f"{log.day} 的卖出成交与完整交易对不上")
    trade_of = dict(zip(map(id, sells), log.trades, strict=True))
    for fill in log.fills:
        signal: Signal | None = None
        if not _is_delisting(fill):
            signal = _signal_of(s, book, (fill.side, fill.symbol, fill.signal_day))
            mark_signal(s, signal, SignalStatus.EXECUTED, log.day)
        gross = fill.shares * fill.price
        fees = fill.amount - gross if fill.side is Side.BUY else gross - fill.amount
        s.add(
            LedgerEntry(
                book_id=book.id,
                user_id=book.user_id,
                kind=fill.side.value,
                symbol=fill.symbol,
                trade_day=fill.day,
                effective_day=fill.day,
                shares=fill.shares,
                price=fill.price,
                fees=fees,
                fees_estimated=False,
                signal_id=None if signal is None else signal.id,
                origin="signal",
                created_by_user_id=book.user_id,
                note=_DELISTING_FILL_NOTE if signal is None else "",
            )
        )
        if fill.side is Side.SELL:
            trade = trade_of[id(fill)]
            s.add(
                TradeRow(
                    book_id=book.id,
                    user_id=book.user_id,
                    symbol=trade.symbol,
                    entry_day=trade.entry_day,
                    exit_day=trade.exit_day,
                    shares=fill.shares,
                    cost=trade.cost,
                    proceeds=trade.proceeds,
                    cash_received=0.0,  # 系统账分红按再投资折算股数
                    exit_reason=trade.exit_reason.value,
                    holding_days=trade.holding_days,
                    entry_metrics=metrics_json(trade.entry_metrics),
                    origin="signal",
                    signal_ref_price=None if signal is None else signal.ref_price,
                    fill_price=fill.price,
                )
            )


def _void_buys(
    s: Session, book: Book, log: DayLog, pending: list[OrderKey], state: EngineState
) -> None:
    """作废没能成交的买单；退市时被引擎直接移除的挂单（买或卖）也作废。"""
    for order, reason in zip(log.expired_buys, log.expired_reasons, strict=True):
        signal = _signal_of(s, book, (order.side, order.symbol, order.signal_day))
        mark_signal(s, signal, SignalStatus.VOIDED, log.day, _VOID_NOTES[reason])
    remaining = {(o.side, o.symbol, o.signal_day) for o in [*state.sell_orders, *state.buy_orders]}
    resolved = {(f.side, f.symbol, f.signal_day) for f in log.fills}
    resolved |= {(o.side, o.symbol, o.signal_day) for o in log.expired_buys}
    for key in pending:
        if key not in remaining and key not in resolved:
            mark_signal(s, _signal_of(s, book, key), SignalStatus.VOIDED, log.day, _DELISTED_NOTE)


def _record_new_signals(
    s: Session,
    book: Book,
    log: DayLog,
    state: EngineState,
    panel: FeaturePanel,
    calendar: TradingCalendar,
    valid_days: int,
) -> None:
    """当晚新下的卖单、买单各记一条"已生成"信号（系统账不发邮件）。参考价为当日收盘价。"""
    day = log.day
    for order in log.new_sell_orders:
        create_signal(
            s,
            book,
            Side.SELL,
            order.symbol,
            day,
            order.shares,
            panel.last_close(day, order.symbol),
            order.reason,
            order.exit_reason,
            {},
            None,  # 卖单不过期
        )
    for order in log.new_buy_orders:
        candidate = state.candidates[order.symbol]
        create_signal(
            s,
            book,
            Side.BUY,
            order.symbol,
            day,
            order.shares,
            panel.last_close(day, order.symbol),
            order.reason,
            None,
            candidate.metrics,
            _valid_until(calendar, day, valid_days),
        )


def advance_system_book(
    s: Session,
    sub: Subscription,
    cfg: AppConfig,
    panel: FeaturePanel,
    calendar: TradingCalendar,
    day: date,
) -> DayLog | None:
    """把订阅的系统账推进到交易日 day 并落库；已处理过（processed_through >= day）则返回 None。

    首次推进时为全部现金（订阅本金），之后必须按交易日逐日推进，跳过交易日会报错。
    参数取订阅当前参数（参数修改从下一次推进起生效，账本不重置）。
    """
    book = repo.get_book_of_kind(s, sub.user_id, sub.id, "system")
    if book.processed_through is not None and book.processed_through >= day:
        return None
    if sub.status != "active":
        raise ValidationError(f"订阅当前状态为 {sub.status}，不推进系统账")
    if sub.strategy_name != STRATEGY_NAME:
        raise ValidationError(f"不支持的策略 {sub.strategy_name}")
    if not calendar.is_trading_day(day):
        raise ValidationError(f"{day} 不是交易日")
    merged = subscription_config(cfg, sub)
    costs = CostModel(merged.costs)
    risk = RiskManager(merged.risk, calendar, costs)
    state, prev_day = _load(s, sub, book, risk, calendar, day)
    pending = [(o.side, o.symbol, o.signal_day) for o in [*state.sell_orders, *state.buy_orders]]
    valid_days = merged.backtest.buy_order_valid_days
    log = step_day(
        state,
        panel,
        HighDividendLow(merged.strategy, merged.universe),
        risk,
        costs,
        calendar,
        day,
        prev_day,
        sub.initial_cash,
        valid_days,
    )
    _save_state(s, book, state, risk, day)
    s.add(
        BookDay(
            book_id=book.id,
            user_id=book.user_id,
            day=day,
            equity=log.equity,
            cash=log.cash,
            unit_nav=log.equity / sub.initial_cash,
        )
    )
    _record_fills(s, book, log)
    _void_buys(s, book, log, pending, state)
    _record_new_signals(s, book, log, state, panel, calendar, valid_days)
    gap = merged.service.rejection_episode_gap_days
    for rejection in log.rejections:
        record_rejection(s, book, rejection, day, calendar, gap)
    for event in log.risk_events:
        s.add(
            RiskEventRow(
                book_id=book.id,
                user_id=book.user_id,
                day=event.day,
                kind=event.kind.value,
                drawdown=event.drawdown,
                message=event.message,
            )
        )
    book.processed_through = day
    s.flush()
    return log
