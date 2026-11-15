"""手动账：由只追加的流水重放出持仓、现金、份额净值与风控状态（纯函数，不碰数据库）。

流水是唯一的事实来源。录入或作废一条过去日期的流水后，调用方重新调用 replay 即可得到
从该日起全部重算后的结果。逐日按以下顺序推进：

1. 除权：除权日之前已持有的股数获得现金分红（进现金，并累加到持仓的 cash_received）与送转股。
2. 应用当日流水：trade_day 为当日的买入、卖出、入金、出金，effective_day 为当日的转入；
   同一天的流水按 entries 中的先后顺序应用（调用方按 id 升序传入）。被作废的流水与作废记录本身跳过。
3. 收盘计值：总资产 = 现金 + Σ 股数 * 收盘价；持有天数加一、更新最高收益（当日转入的不重复加）。
4. 份额净值：当日的入金、出金、转入（按当日收盘市值）不改变当日份额净值，只改变份额。
5. 风控：用份额净值驱动回撤预警与暂停。
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from typing import Protocol

from qt.calendar import TradingCalendar
from qt.config import RiskConfig
from qt.costs import LOT_SIZE, CostModel
from qt.models import ExitReason, Position
from qt.risk import RiskEvent, RiskManager, RiskState
from qt.service.errors import LedgerInconsistentError, ValidationError

# 股数与金额的浮点容差
_EPS = 1e-6


class EntryKind(StrEnum):
    """流水种类。"""

    BUY = "buy"
    SELL = "sell"
    TRANSFER_IN = "transfer_in"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    VOID = "void"


@dataclass(frozen=True)
class Entry:
    """一条手动账流水。

    入金、出金的金额记在 price 上（shares 不使用）；转入的 price 为使用者填写的成本价。
    作废记录只用 voids_entry_id，trade_day 为作废操作的日期，不参与重放。
    """

    id: int
    kind: EntryKind
    trade_day: date  # 成交日或买入日
    effective_day: date  # 进入净值的交易日；转入为录入日起第一个交易日，其余等于 trade_day
    symbol: str | None
    shares: float
    price: float  # 转入为成本价；入金、出金为金额
    fees: float
    origin: str | None  # "signal" | "self"
    exit_reason: ExitReason | None  # 卖出关联的信号原因，无关联为 None
    voids_entry_id: int | None
    industry: str
    entry_metrics: Mapping[str, float]

    @property
    def apply_day(self) -> date:
        """这条流水在重放中生效的交易日。"""
        return self.effective_day if self.kind is EntryKind.TRANSFER_IN else self.trade_day


@dataclass(frozen=True)
class CorporateAction:
    """由分红表推算出来的展示用流水，不入库。cash 为现金分红总额，bonus_shares 为送转股数。"""

    day: date
    symbol: str
    cash: float
    bonus_shares: float


class Market(Protocol):
    """重放需要的行情数据。"""

    def last_close(self, day: date, symbol: str) -> float:
        """day 当日（停牌则为此前最近一个交易日）的不复权收盘价。"""
        ...

    def dividends(self, symbol: str) -> Sequence[tuple[date, float, float]]:
        """(除权日, 每股现金, 每股送转) 列表。"""
        ...

    def delisted_before(self, day: date, symbol: str) -> bool:
        """day 之前是否已经退市。"""
        ...

    def industry(self, symbol: str) -> str:
        """股票所属行业，用于填写 Entry.industry。"""
        ...


@dataclass(frozen=True)
class ManualTrade:
    """一笔卖出（全部或部分），字段同 trades 表。cost、cash_received 为卖出部分按比例分摊的值。

    exit_reason 为 None 表示没有关联卖出信号（入库时记为 manual）；
    signal_ref_price 重放时无从得知，恒为 None，由关联信号的调用方填写。
    """

    symbol: str
    entry_day: date
    exit_day: date
    shares: float
    cost: float
    proceeds: float
    cash_received: float
    exit_reason: ExitReason | None
    holding_days: int
    entry_metrics: Mapping[str, float]
    origin: str
    signal_ref_price: float | None
    fill_price: float


@dataclass(frozen=True)
class ManualState:
    """重放结果。days、equity、cash_history、unit_nav 逐日一一对应。"""

    positions: dict[str, Position]
    origins: dict[str, str]  # 股票 -> "signal" | "self"
    cash: float
    units: float
    days: list[date]
    equity: list[float]
    unit_nav: list[float]
    trades: list[ManualTrade]
    actions: list[CorporateAction]
    risk_events: list[RiskEvent]
    risk: RiskState
    cash_history: list[float]  # 每日日终现金


class LedgerReplayError(ValueError):
    """replay 发现流水不一致（超卖、现金为负、日期不在账本交易日内）。"""


class _LedgerProblem(Exception):
    """重放中发现流水不一致。kind 为 position（持仓为负）、cash（现金为负）或 other。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class _Replayer:
    """一次重放的可变中间状态。"""

    def __init__(
        self,
        market: Market,
        calendar: TradingCalendar,
        risk: RiskManager,
        start_cash: float,
    ) -> None:
        self.market = market
        self.calendar = calendar
        self.risk = risk
        self.positions: dict[str, Position] = {}
        self.origins: dict[str, str] = {}
        self.cash = start_cash
        self.units = start_cash
        self.nav = 1.0
        self.days: list[date] = []
        self.equity: list[float] = []
        self.unit_nav: list[float] = []
        self.cash_history: list[float] = []
        self.trades: list[ManualTrade] = []
        self.actions: list[CorporateAction] = []
        self.risk_events: list[RiskEvent] = []
        self._dividends: dict[str, Sequence[tuple[date, float, float]]] = {}

    def dividends_in(self, symbol: str, after: date | None, through: date) -> tuple[float, float]:
        """除权日落在 (after, through] 内的每股现金与每股送转之和。"""
        if symbol not in self._dividends:
            self._dividends[symbol] = self.market.dividends(symbol)
        cash = bonus = 0.0
        for ex_day, cash_ps, bonus_ps in self._dividends[symbol]:
            if (after is None or ex_day > after) and ex_day <= through:
                cash += cash_ps
                bonus += bonus_ps
        return cash, bonus

    def step(self, day: date, prev_day: date | None, entries: Sequence[Entry]) -> None:
        """推进一个交易日（五步见模块说明）。"""
        self._apply_dividends(day, prev_day)
        inflow, fresh = self._apply_entries(day, entries)
        if self.cash < -_EPS:
            raise _LedgerProblem("cash", f"{day} 的支出超过现金：当日日终现金为 {self.cash:.2f} 元")
        equity = self._mark_to_market(day, fresh)
        if self.units > _EPS:
            self.nav = (equity - inflow) / self.units
        else:
            self.units = 0.0  # 资金已全部取出：沿用上一日净值，等下一笔入金重新建份额
        self.units += inflow / self.nav
        self.days.append(day)
        self.equity.append(equity)
        self.unit_nav.append(self.nav)
        self.cash_history.append(self.cash)
        self.risk_events.extend(self.risk.on_day_close(day, self.nav))

    def _apply_dividends(self, day: date, prev_day: date | None) -> None:
        """第 1 步 除权：只作用于此刻（当日流水之前）已持有的股数。"""
        for symbol, pos in self.positions.items():
            cash_ps, bonus_ps = self.dividends_in(symbol, prev_day, day)
            if cash_ps == 0 and bonus_ps == 0:
                continue
            cash = pos.shares * cash_ps
            bonus = pos.shares * bonus_ps
            self.cash += cash
            pos.cash_received += cash
            pos.shares += bonus
            self.actions.append(CorporateAction(day, symbol, cash, bonus))

    def _apply_entries(self, day: date, entries: Sequence[Entry]) -> tuple[float, set[str]]:
        """第 2 步 应用当日流水，返回 (当日净流入, 当日转入的股票)。"""
        inflow = 0.0
        fresh: set[str] = set()
        for e in entries:
            if e.kind is EntryKind.DEPOSIT:
                self.cash += e.price
                inflow += e.price
            elif e.kind is EntryKind.WITHDRAW:
                self.cash -= e.price
                inflow -= e.price
            elif e.kind is EntryKind.BUY:
                self._buy(e)
            elif e.kind is EntryKind.SELL:
                self._sell(day, e)
            elif e.kind is EntryKind.TRANSFER_IN:
                inflow += self._transfer_in(day, e)
                fresh.add(_symbol(e))
        return inflow, fresh

    def _buy(self, e: Entry) -> None:
        symbol = _symbol(e)
        amount = e.price * e.shares + e.fees
        self.cash -= amount
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(
                symbol=symbol,
                shares=e.shares,
                cost=amount,
                entry_day=e.trade_day,
                industry=e.industry,
                entry_metrics=e.entry_metrics,
            )
            self.origins[symbol] = e.origin or "self"
            return
        pos.shares += e.shares
        pos.cost += amount
        pos.entry_day = min(pos.entry_day, e.trade_day)

    def _sell(self, day: date, e: Entry) -> None:
        symbol = _symbol(e)
        pos = self.positions.get(symbol)
        held = 0.0 if pos is None else pos.shares
        if pos is None or e.shares > held + _EPS:
            raise _LedgerProblem(
                "position", f"{day} 卖出 {symbol} {e.shares:g} 股超过持仓（当时持有 {held:g} 股）"
            )
        proceeds = e.price * e.shares - e.fees
        fraction = min(e.shares / pos.shares, 1.0)
        cost = pos.cost * fraction
        cash_received = pos.cash_received * fraction
        self.cash += proceeds
        self.trades.append(
            ManualTrade(
                symbol=symbol,
                entry_day=pos.entry_day,
                exit_day=day,
                shares=e.shares,
                cost=cost,
                proceeds=proceeds,
                cash_received=cash_received,
                exit_reason=e.exit_reason,
                holding_days=pos.days_held,
                entry_metrics=pos.entry_metrics,
                origin=self.origins[symbol],
                signal_ref_price=None,
                fill_price=e.price,
            )
        )
        if pos.shares - e.shares <= _EPS:
            del self.positions[symbol]
            del self.origins[symbol]
            if e.exit_reason is not None:
                self.risk.note_exit(symbol, day, e.exit_reason)
            return
        pos.shares -= e.shares
        pos.cost -= cost
        pos.cash_received -= cash_received

    def _transfer_in(self, day: date, e: Entry) -> float:
        """转入持仓：不动现金，按使用者成本建仓并补算持有天数与最高收益；返回当日收盘市值。"""
        symbol = _symbol(e)
        if symbol in self.positions:
            raise _LedgerProblem(
                "other", f"{day} 转入 {symbol} 时它已在持仓中，不能重复转入，请改为录入买入"
            )
        self.positions[symbol] = Position(
            symbol=symbol,
            shares=e.shares,
            cost=e.price * e.shares,
            entry_day=e.trade_day,
            industry=e.industry,
            entry_metrics=e.entry_metrics,
            days_held=len(self.calendar.between(e.trade_day, day)),
            peak_return=self._backfill_peak(day, e),
        )
        self.origins[symbol] = e.origin or "self"
        return e.shares * self.market.last_close(day, symbol)

    def _backfill_peak(self, day: date, e: Entry) -> float:
        """用买入日到当日的收盘价补算最高收益（只看价格）。

        使用者填写的股数视为当日的持股：按期间送转倒推出各日的持股再乘收盘价。
        转入前的现金分红不计入，与转入持仓的 cash_received 从 0 开始保持一致。
        """
        symbol = _symbol(e)
        cost = e.price * e.shares
        period = self.calendar.between(e.trade_day, day)
        growth = 1.0
        for prev, d in pairwise(period):
            growth *= 1 + self.dividends_in(symbol, prev, d)[1]
        shares = e.shares / growth
        peak = 0.0
        for i, d in enumerate(period):
            if i > 0:
                shares *= 1 + self.dividends_in(symbol, period[i - 1], d)[1]
            peak = max(peak, shares * self.market.last_close(d, symbol) / cost - 1)
        return peak

    def _mark_to_market(self, day: date, fresh: set[str]) -> float:
        """第 3 步 收盘计值，返回当日总资产。"""
        equity = self.cash
        for symbol, pos in self.positions.items():
            close = self.market.last_close(day, symbol)
            equity += pos.shares * close
            if symbol not in fresh:
                pos.days_held += 1
                pos.peak_return = max(pos.peak_return, pos.ret(close))
        return equity


def _symbol(e: Entry) -> str:
    if e.symbol is None:
        raise _LedgerProblem("other", f"流水 {e.id}（{e.kind}）缺少股票代码")
    return e.symbol


def _replay(
    entries: Sequence[Entry],
    market: Market,
    calendar: TradingCalendar,
    risk_cfg: RiskConfig,
    costs: CostModel,
    start_cash: float,
    start_day: date,
    through: date,
) -> ManualState:
    """重放；流水不一致时抛 _LedgerProblem。"""
    voided = {e.voids_entry_id for e in entries if e.kind is EntryKind.VOID}
    live = [e for e in entries if e.kind is not EntryKind.VOID and e.id not in voided]
    days = calendar.between(start_day, through)
    day_set = set(days)
    by_day: dict[date, list[Entry]] = {}
    for e in live:
        if e.apply_day <= through and e.apply_day not in day_set:
            raise _LedgerProblem(
                "other", f"流水 {e.id} 的日期 {e.apply_day} 不是账本自 {start_day} 起的交易日"
            )
        by_day.setdefault(e.apply_day, []).append(e)

    risk = RiskManager(risk_cfg, calendar, costs)
    book = _Replayer(market, calendar, risk, start_cash)
    prev: date | None = None
    for day in days:
        book.step(day, prev, by_day.get(day, []))
        prev = day
    return ManualState(
        positions=book.positions,
        origins=book.origins,
        cash=book.cash,
        units=book.units,
        days=book.days,
        equity=book.equity,
        unit_nav=book.unit_nav,
        trades=book.trades,
        actions=book.actions,
        risk_events=book.risk_events,
        risk=risk.dump_state(),
        cash_history=book.cash_history,
    )


def replay(
    entries: Sequence[Entry],
    market: Market,
    calendar: TradingCalendar,
    risk_cfg: RiskConfig,
    costs: CostModel,
    start_cash: float,
    start_day: date,
    through: date,
) -> ManualState:
    """从账本起始日重放到 through（含），返回此刻的手动账状态。

    流水不一致（超卖、现金为负、日期不在账本交易日内）时抛 LedgerReplayError（ValueError 的子类）；
    经 validate_entry 校验过的流水不会出现这种情况。
    """
    try:
        return _replay(entries, market, calendar, risk_cfg, costs, start_cash, start_day, through)
    except _LedgerProblem as p:
        raise LedgerReplayError(f"手动账流水不一致：{p.message}") from p


def _check_void(entries: Sequence[Entry], new: Entry) -> None:
    target = next((e for e in entries if e.id == new.voids_entry_id), None)
    if target is None:
        raise ValidationError(f"要作废的流水 {new.voids_entry_id} 不存在")
    if target.kind is EntryKind.VOID:
        raise ValidationError("不能作废一条作废记录")
    if any(e.kind is EntryKind.VOID and e.voids_entry_id == target.id for e in entries):
        raise ValidationError(f"流水 {target.id} 已作废，不能重复作废")


def _check_fields(
    new: Entry,
    market: Market,
    calendar: TradingCalendar,
    start_day: date,
    today: date,
    backdate_limit_days: int,
    known_symbols: AbstractSet[str],
) -> None:
    """不依赖重放的逐项检查。"""
    kind = new.kind
    with_symbol = kind in (EntryKind.BUY, EntryKind.SELL, EntryKind.TRANSFER_IN)
    if with_symbol and new.symbol not in known_symbols:
        raise ValidationError(f"股票代码不存在：{new.symbol}")
    day = new.trade_day
    if day > today:
        raise ValidationError(f"日期 {day} 不能晚于今天（{today}）")
    if kind is EntryKind.TRANSFER_IN and day < calendar.first_day:
        raise ValidationError(
            f"转入的买入日期 {day} 早于行情数据的起点，最早可填 {calendar.first_day}"
        )
    if not calendar.is_trading_day(day):
        raise ValidationError(f"{day} 不是交易日")
    if kind is not EntryKind.TRANSFER_IN:
        ago = calendar.count_between(day, today)
        if ago > backdate_limit_days:
            raise ValidationError(
                f"日期最多可以早于今天 {backdate_limit_days} 个交易日，"
                f"{day} 早于今天 {ago} 个交易日"
            )
    if new.apply_day < start_day:
        raise ValidationError(f"生效日期 {new.apply_day} 早于账本起始日 {start_day}")
    if with_symbol:
        if not new.shares > 0:
            raise ValidationError("股数必须为正")
        lots = new.shares / LOT_SIZE
        if kind is not EntryKind.SELL and abs(lots - round(lots)) > _EPS:
            raise ValidationError(f"买入和转入的股数必须是 {LOT_SIZE} 的整数倍")
        if not new.price > 0:
            raise ValidationError("价格必须为正")
    elif not new.price > 0:
        raise ValidationError("金额必须为正")
    if not new.fees >= 0:
        raise ValidationError("费用不能为负")
    if kind is EntryKind.TRANSFER_IN:
        _check_transfer_effective_day(new, calendar, today)
    symbol = new.symbol
    buying = kind in (EntryKind.BUY, EntryKind.TRANSFER_IN)
    if buying and symbol is not None and market.delisted_before(new.apply_day, symbol):
        raise ValidationError(f"{symbol} 在 {new.apply_day} 之前已退市，不能买入或转入")


def _check_transfer_effective_day(new: Entry, calendar: TradingCalendar, today: date) -> None:
    """转入的生效日：不早于买入日的交易日，且不晚于今天起（含）的第一个交易日。"""
    upcoming = calendar.between(today, date.max)[:1]
    latest = upcoming[0] if upcoming else today
    eff = new.effective_day
    if eff < new.trade_day or eff > latest or not calendar.is_trading_day(eff):
        raise ValidationError(
            f"转入的生效日 {eff} 必须是不早于买入日 {new.trade_day}、不晚于 {latest} 的交易日"
        )


def validate_entry(
    entries: Sequence[Entry],
    new: Entry,
    market: Market,
    calendar: TradingCalendar,
    risk_cfg: RiskConfig,
    costs: CostModel,
    start_cash: float,
    start_day: date,
    *,
    today: date,
    backdate_limit_days: int,
    known_symbols: AbstractSet[str],
) -> None:
    """校验一条待追加的流水，不合法时抛 ValidationError（消息可直接展示给使用者）。

    entries 为账本现有的全部流水（按 id 升序）。除逐项检查外，把新流水追加到末尾后从账本
    起始日重放到"今天与全部流水生效日中较晚者"：任何一天出现超卖、现金为负、转入时已持有
    都拒绝。这样同时覆盖"卖出不超过当日持仓""出金不超过当日现金"、回溯录入让之后某笔卖出
    超卖、作废后出现负持仓或负现金，以及与尚未生效的转入冲突。
    现有流水在不加新流水时就已不一致（如数据源修订了已入账的分红）：加上新流水之后能在同一区间
    重放通过的（一笔入金或一次作废把账本修好），照常接受；仍不一致抛 LedgerInconsistentError
    （不归咎于本次录入），消息说明问题与修复办法。
    """
    if new.kind is EntryKind.VOID:
        _check_void(entries, new)
    else:
        _check_fields(new, market, calendar, start_day, today, backdate_limit_days, known_symbols)
    # 重放到今天与所有已录入流水（含尚未生效的转入）生效日中的较晚者
    voided = {e.voids_entry_id for e in [*entries, new] if e.kind is EntryKind.VOID}
    live = [e for e in [*entries, new] if e.kind is not EntryKind.VOID and e.id not in voided]
    through = max([today, *(e.apply_day for e in live)])
    try:
        _replay(entries, market, calendar, risk_cfg, costs, start_cash, start_day, through)
    except _LedgerProblem as old:
        try:
            _replay(
                [*entries, new], market, calendar, risk_cfg, costs, start_cash, start_day, through
            )
        except _LedgerProblem as p:
            raise LedgerInconsistentError(
                f"账本现有流水已不一致（与本次录入无关）：{old.message}；加上本次录入后仍不一致："
                f"{p.message}。录入一笔足够的入金（qt admin cash deposit），或作废出错的流水"
                "（qt admin entry void），把账本修好之后再录入其他流水"
            ) from p
        return  # 本次录入把账本修好了
    try:
        _replay([*entries, new], market, calendar, risk_cfg, costs, start_cash, start_day, through)
    except _LedgerProblem as p:
        if new.kind is not EntryKind.VOID:
            raise ValidationError(p.message) from p
        what = {"position": "持仓为负", "cash": "现金为负"}.get(p.kind, "流水不一致")
        raise ValidationError(f"作废后{what}：{p.message}") from p
