"""按日回测引擎。

每个交易日严格按七步执行（各写成一个私有函数，主循环 run_backtest 只负责按顺序调用）：
    1 退市清算  2 除权调整  3 撮合挂单  4 收盘计值  5 风控记账  6 卖出信号  7 买入候选与定仓
信号在收盘后产生，次日开盘才撮合，因此当日买入的股票当日不会被卖出（T+1）。
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

import pandas as pd

from qt.calendar import TradingCalendar
from qt.costs import LOT_SIZE, CostModel
from qt.features import Bar, FeaturePanel
from qt.models import BuyCandidate, ExitReason, Order, Position, Side, Trade
from qt.risk import Rejection, RiskEvent, RiskEventKind, RiskManager
from qt.strategy.base import Strategy

_EPS = 1e-9  # 价格与涨跌停价比较的容差


@dataclass(frozen=True)
class BacktestResult:
    """回测结果。equity、cash 以交易日（datetime.date）为索引。"""

    equity: "pd.Series[float]"  # 每日总资产 = 现金 + 持仓按最后收盘价计值
    cash: "pd.Series[float]"
    trades: list[Trade]
    open_positions: list[Position]  # 回测结束时仍持有的仓位
    rejections: list[Rejection]
    risk_events: list[RiskEvent]
    expired_buys: list[Order]  # 超过有效期仍未成交（或买不起一手）的买单


@dataclass
class _State:
    """回测过程中的可变状态。positions 按买入先后保持插入顺序，保证结果可复现。"""

    cash: float
    positions: dict[str, Position] = field(default_factory=lambda: {})
    sell_orders: list[Order] = field(default_factory=lambda: [])  # 待成交卖单，按创建顺序
    buy_orders: list[Order] = field(default_factory=lambda: [])  # 待成交买单，按创建顺序
    candidates: dict[str, BuyCandidate] = field(default_factory=lambda: {})  # 买单对应的候选
    equity: dict[date, float] = field(default_factory=lambda: {})
    cash_by_day: dict[date, float] = field(default_factory=lambda: {})
    trades: list[Trade] = field(default_factory=lambda: [])
    rejections: list[Rejection] = field(default_factory=lambda: [])
    risk_events: list[RiskEvent] = field(default_factory=lambda: [])
    expired_buys: list[Order] = field(default_factory=lambda: [])


_CENT = Decimal("0.01")
_MULTIPLIERS = {
    # (是否 ST, 是否涨停): 乘数。普通股 ±10%，ST ±5%
    (False, True): Decimal("1.10"),
    (False, False): Decimal("0.90"),
    (True, True): Decimal("1.05"),
    (True, False): Decimal("0.95"),
}


def _cents(value: Decimal) -> Decimal:
    """按分四舍五入（逢五进一），不用 Python 的银行家舍入。"""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def limit_prices(prev_close: float, is_st: bool) -> tuple[float, float]:
    """(涨停价, 跌停价)，全程用十进制精确计算，避免浮点乘积差一分钱。

    交易所的参考价以分为单位：先把前收盘价按分四舍五入（除权日的前收盘价是
    按复权因子折算过的，不一定恰好是整分），再乘以 1.10 / 0.90（ST 为 1.05 / 0.95），
    结果同样按分逢五进一。例如前收 7.85：7.85 * 0.9 = 7.065 -> 跌停价 7.07；
    若直接用浮点乘积 7.0649999999999995 会得到 7.06，让本该跌停打不开的卖单成交。
    """
    ref = _cents(Decimal(repr(prev_close)))
    up = _cents(ref * _MULTIPLIERS[(is_st, True)])
    down = _cents(ref * _MULTIPLIERS[(is_st, False)])
    return float(up), float(down)


def _limits(bar: Bar) -> tuple[float, float]:
    """某根行情对应的 (涨停价, 跌停价)。"""
    return limit_prices(bar.prev_close, bar.is_st)


def _close_position(
    state: _State,
    risk: RiskManager,
    day: date,
    pos: Position,
    proceeds: float,
    reason: ExitReason,
) -> None:
    """平仓：现金入账、记录成交、通知风控（用于冷却），并移出持仓。"""
    state.cash += proceeds
    state.trades.append(
        Trade(
            symbol=pos.symbol,
            entry_day=pos.entry_day,
            exit_day=day,
            cost=pos.cost,
            proceeds=proceeds,
            exit_reason=reason,
            holding_days=pos.days_held,
            entry_metrics=pos.entry_metrics,
        )
    )
    risk.note_exit(pos.symbol, day, reason)
    del state.positions[pos.symbol]


def _liquidate_delisted(
    state: _State,
    panel: FeaturePanel,
    costs: CostModel,
    risk: RiskManager,
    day: date,
    prev_day: date | None,
) -> None:
    """第 1 步 退市清算：逐个日历日扫描 (上一交易日, 今天]，因为退市日可能不是交易日。

    退市股票的挂单（买、卖）一律作废，不论是否持有。持仓按上一交易日的收盘价卖出
    （开盘前已知的最后价格，回收的资金可以用于今天的买入），仍扣除滑点与费用。
    回测首日没有上一交易日，只扫描当天（此时不可能有持仓）。
    """
    scan = day if prev_day is None else prev_day + timedelta(days=1)
    while scan <= day:
        for symbol in panel.delisted_on(scan):
            state.sell_orders = [o for o in state.sell_orders if o.symbol != symbol]
            state.buy_orders = [o for o in state.buy_orders if o.symbol != symbol]
            state.candidates.pop(symbol, None)
            pos = state.positions.get(symbol)
            if pos is None:
                continue
            assert prev_day is not None  # 持仓最早出现在回测第二天，必有上一交易日
            price = panel.last_close(prev_day, symbol)
            _close_position(
                state,
                risk,
                day,
                pos,
                costs.sell_proceeds(price, pos.shares, day),
                ExitReason.DELISTED,
            )
        scan += timedelta(days=1)


def _apply_ex_dividend(state: _State, panel: FeaturePanel, day: date) -> None:
    """第 2 步 除权调整：今天之前买入的持仓按 factor_ratio 放大股数（等同分红再投资）。

    今天才买入的持仓此时还不存在（买单在第 3 步撮合），自然不会被调整。
    """
    for pos in state.positions.values():
        bar = panel.bar(day, pos.symbol)
        if bar is not None:
            pos.scale_shares(bar.factor_ratio)


def _match_sells(
    state: _State,
    panel: FeaturePanel,
    costs: CostModel,
    risk: RiskManager,
    day: date,
) -> None:
    """第 3 步（卖）：以开盘价成交；停牌或开盘跌停则顺延到下一交易日，卖单不过期。"""
    remaining: list[Order] = []
    for order in state.sell_orders:
        pos = state.positions.get(order.symbol)
        if pos is None:
            continue
        bar = panel.bar(day, order.symbol)
        if bar is None or bar.open <= _limits(bar)[1] + _EPS:
            remaining.append(order)
            continue
        # 卖出成交时的股数（除权后可能带小数）才是实际股数，而非下单时的股数
        proceeds = costs.sell_proceeds(bar.open, pos.shares, day)
        assert order.exit_reason is not None
        _close_position(state, risk, day, pos, proceeds, order.exit_reason)
    state.sell_orders = remaining


def _match_buys(
    state: _State,
    panel: FeaturePanel,
    costs: CostModel,
    calendar: TradingCalendar,
    day: date,
    valid_days: int,
) -> None:
    """第 3 步（买）：以开盘价成交；停牌或开盘涨停则顺延，自信号日起满有效期仍未成交则作废。

    现金不足时按整手减量，减到 0 手则丢弃并记入 expired_buys。
    """
    remaining: list[Order] = []
    for order in state.buy_orders:
        candidate = state.candidates[order.symbol]
        bar = panel.bar(day, order.symbol)
        if bar is None or bar.open >= _limits(bar)[0] - _EPS:
            if calendar.count_between(order.signal_day, day) >= valid_days:
                state.expired_buys.append(order)
                del state.candidates[order.symbol]
            else:
                remaining.append(order)
            continue
        lots = costs.max_lots(bar.open, max(state.cash, 0.0))
        shares = min(order.shares, lots * LOT_SIZE)
        if shares <= 0:
            state.expired_buys.append(order)
            del state.candidates[order.symbol]
            continue
        cost = costs.buy_cost(bar.open, shares)
        state.cash -= cost
        state.positions[order.symbol] = Position(
            symbol=order.symbol,
            shares=shares,
            cost=cost,
            entry_day=day,
            industry=candidate.industry,
            entry_metrics=candidate.metrics,
        )
        del state.candidates[order.symbol]
    state.buy_orders = remaining


def _mark_to_market(state: _State, panel: FeaturePanel, day: date) -> float:
    """第 4 步 收盘计值：持有天数加一、更新峰值收益；停牌股票按最后收盘价计值。

    返回当日总资产并记录 equity 与 cash。
    """
    total = state.cash
    for pos in state.positions.values():
        close = panel.last_close(day, pos.symbol)
        pos.days_held += 1
        pos.peak_return = max(pos.peak_return, pos.ret(close))
        total += pos.shares * close
    state.equity[day] = total
    state.cash_by_day[day] = state.cash
    return total


def _update_risk(
    state: _State, risk: RiskManager, day: date, equity: float, initial_cash: float
) -> None:
    """第 5 步 风控记账：用净值（总资产 / 初始资金）驱动回撤预警与暂停。

    暂停买入生效的当晚，所有待成交买单一并作废（记入 expired_buys），
    保证暂停期间不会有新仓位成交；已持有的仓位和卖单不受影响。
    """
    events = risk.on_day_close(day, equity / initial_cash)
    state.risk_events.extend(events)
    if any(e.kind is RiskEventKind.PAUSE for e in events):
        state.expired_buys.extend(state.buy_orders)
        state.buy_orders = []
        state.candidates.clear()


def _create_sell_orders(
    state: _State, strategy: Strategy, snapshot: pd.DataFrame, day: date
) -> None:
    """第 6 步 卖出信号：每个持仓最多一张卖单，已有未成交卖单的不重复下单。"""
    signals = strategy.sell_signals(day, snapshot, list(state.positions.values()))
    for signal in signals:
        pos = state.positions.get(signal.symbol)
        if pos is None or any(o.symbol == signal.symbol for o in state.sell_orders):
            continue
        state.sell_orders.append(
            Order(signal.symbol, Side.SELL, pos.shares, day, signal.detail, signal.reason)
        )


def _create_buy_orders(
    state: _State,
    panel: FeaturePanel,
    strategy: Strategy,
    risk: RiskManager,
    snapshot: pd.DataFrame,
    day: date,
    equity: float,
) -> None:
    """第 7 步 买入候选与定仓：持仓与待成交买单的股票都算已持有。

    暂停买入期间直接跳过本步，不向策略要候选，也不调用 size_buys，
    因此回测结果的 rejections 里不会出现 paused 拒绝记录。
    """
    if not risk.buying_allowed():
        return
    held = frozenset(state.positions) | frozenset(o.symbol for o in state.buy_orders)
    candidates = strategy.buy_candidates(day, snapshot, held)
    closes: dict[str, float] = {}
    for c in candidates:
        closes[c.symbol] = _snapshot_close(snapshot, c)
    for symbol in [*state.positions, *(o.symbol for o in state.buy_orders)]:
        closes[symbol] = panel.last_close(day, symbol)
    orders, rejections = risk.size_buys(
        day,
        candidates,
        equity,
        state.cash,
        list(state.positions.values()),
        closes,
        list(state.buy_orders),
    )
    state.rejections.extend(rejections)
    by_symbol = {c.symbol: c for c in candidates}
    for order in orders:
        state.buy_orders.append(order)
        state.candidates[order.symbol] = by_symbol[order.symbol]


def _snapshot_close(snapshot: pd.DataFrame, candidate: BuyCandidate) -> float:
    """候选的定仓价：优先用当日快照收盘价，快照里没有则用候选自带的收盘价。"""
    if candidate.symbol in snapshot.index:
        return float(cast(float, snapshot.at[candidate.symbol, "close"]))
    return candidate.close


def run_backtest(
    panel: FeaturePanel,
    strategy: Strategy,
    risk: RiskManager,
    costs: CostModel,
    calendar: TradingCalendar,
    start: date,
    end: date,
    initial_cash: float,
    buy_order_valid_days: int = 3,
) -> BacktestResult:
    """在 [start, end] 的每个交易日上按七步顺序运行回测。

    回测结束时仍未成交的挂单直接留着，不计入 expired_buys；仍持有的仓位放在 open_positions。
    """
    state = _State(cash=initial_cash)
    days = calendar.between(start, end)
    for i, day in enumerate(days):
        prev_day = days[i - 1] if i > 0 else None
        _liquidate_delisted(state, panel, costs, risk, day, prev_day)  # 1
        _apply_ex_dividend(state, panel, day)  # 2
        _match_sells(state, panel, costs, risk, day)  # 3 先卖后买
        _match_buys(state, panel, costs, calendar, day, buy_order_valid_days)  # 3
        equity = _mark_to_market(state, panel, day)  # 4
        _update_risk(state, risk, day, equity, initial_cash)  # 5
        snapshot = panel.snapshot(day)
        _create_sell_orders(state, strategy, snapshot, day)  # 6
        _create_buy_orders(state, panel, strategy, risk, snapshot, day, equity)  # 7
    index = pd.Index(days)
    return BacktestResult(
        equity=pd.Series(list(state.equity.values()), index=index, dtype="float64"),
        cash=pd.Series(list(state.cash_by_day.values()), index=index, dtype="float64"),
        trades=state.trades,
        open_positions=list(state.positions.values()),
        rejections=state.rejections,
        risk_events=state.risk_events,
        expired_buys=state.expired_buys,
    )
