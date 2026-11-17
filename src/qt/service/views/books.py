"""两本账的展示：净值、现金、持仓、流水、净值序列；以及页面行情需要覆盖的股票集合。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from sqlalchemy.orm import Session

from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig, StrategyConfig
from qt.data.quotes import LiveQuote
from qt.db import repo
from qt.db.models import LedgerEntry
from qt.models import Position
from qt.service import manual_ops
from qt.service.accounts import subscription_config
from qt.service.errors import LedgerInconsistentError, ValidationError
from qt.service.manual_book import CorporateAction, Market
from qt.service.signals import SignalStatus
from qt.service.views.base import (
    KIND_LABELS,
    Names,
    SubscriptionView,
    name_of,
    origin_label,
    subscription_view,
)
from qt.strategy.high_dividend_low import exit_levels

_OPEN = (SignalStatus.CREATED.value, SignalStatus.NOTIFIED.value)


@dataclass(frozen=True)
class BookSummary:
    """一本账截至 as_of 收盘：nav 为总资产（净值），unit_nav 为份额净值（起始为 1）。"""

    nav: float
    unit_nav: float
    cash: float
    holdings_value: float
    holdings_count: int
    as_of: date


@dataclass(frozen=True)
class HoldingRow:
    """一只持仓。cost 为含费用的买入总额；取不到收盘价（没有行情）时 close、value、ret、pnl
    为 None。

    ret、pnl 含持有期间收到的现金分红（cash_received）；stop_price、take_price 为止损位、
    止盈位（手动账才有，移动止盈还没启动时止盈位为 None）。

    price_source 说明 close 是什么价：close 为收盘价（缺省）；live 为盘中价；suspended 为
    盘中报价显示停牌或还没成交时的昨收。后两种只由 apply_quotes 给出，只用于显示。
    """

    symbol: str
    name: str
    shares: float
    cost: float
    cost_price: float
    close: float | None
    value: float | None
    ret: float | None
    pnl: float | None
    origin: str
    origin_label: str
    stop_price: float | None
    take_price: float | None
    cash_received: float = 0.0
    price_source: str = "close"


@dataclass(frozen=True)
class LiveHoldings:
    """按盘中报价改过现价的持仓。quote_time 是用到的报价里最新的时间（没有用到报价为 None）；
    live_count、suspended_count、close_count 分别是用盘中价、标为停牌、仍用收盘价的只数。
    """

    rows: list[HoldingRow]
    quote_time: datetime | None
    live_count: int
    suspended_count: int
    close_count: int


@dataclass(frozen=True)
class EntryRow:
    """一条流水，或由分红表推算出的分红、送转（id 为 None，kind 为 dividend 或 bonus）。

    price：成交价；转入为成本价；入金、出金为金额。amount：入金、出金、分红为金额，
    买入、卖出、转入为股数 * 价格，其余为 0。送转的股数在 shares 里。
    voided 为真表示已被作废；can_void 为真表示可以作废
    （手动账、订阅未关闭、不是作废记录、还没作废）。
    """

    id: int | None
    kind: str
    kind_label: str
    day: date
    effective_day: date
    symbol: str | None
    name: str
    shares: float
    price: float
    amount: float
    fees: float
    fees_estimated: bool
    origin: str | None
    signal_id: int | None
    voids_entry_id: int | None
    note: str
    voided: bool
    can_void: bool


@dataclass(frozen=True)
class NavPoint:
    """每日净值序列里的一天。"""

    day: date
    equity: float
    cash: float
    unit_nav: float


@dataclass(frozen=True)
class SystemBookView:
    """系统账：还没推进过时 summary 为 None、holdings 为空。entries 按录入先后，nav 按日期升序。"""

    subscription: SubscriptionView
    summary: BookSummary | None
    holdings: list[HoldingRow]
    entries: list[EntryRow]
    nav: list[NavPoint]


@dataclass(frozen=True)
class ManualBookView:
    """手动账：entries 含推算出的分红送转，按日期先后（同日先实际流水、后推算记录）。"""

    subscription: SubscriptionView
    summary: BookSummary
    holdings: list[HoldingRow]
    entries: list[EntryRow]


@dataclass(frozen=True)
class ManualBookPage:
    """手动账页面用的结果：流水本身不一致（LedgerInconsistentError）时不抛异常，
    summary 为 None、holdings 为空、error 是服务给出的原因，entries 仍列出全部原始流水（可作废）。
    """

    subscription: SubscriptionView
    summary: BookSummary | None
    holdings: list[HoldingRow]
    entries: list[EntryRow]
    error: str | None


def _holding(
    pos: Position,
    origin: str,
    market: Market | None,
    day: date,
    names: Names | None,
    levels: tuple[float | None, float | None],
) -> HoldingRow:
    close: float | None = None
    if market is not None:
        try:
            close = market.last_close(day, pos.symbol)
        except KeyError:
            close = None
    value = None if close is None else pos.shares * close
    return HoldingRow(
        symbol=pos.symbol,
        name=name_of(names, pos.symbol),
        shares=pos.shares,
        cost=pos.cost,
        cost_price=pos.cost / pos.shares,
        close=close,
        value=value,
        ret=None if close is None else pos.ret(close),
        pnl=None if value is None else value + pos.cash_received - pos.cost,
        origin=origin,
        origin_label=origin_label(origin),
        stop_price=levels[0],
        take_price=levels[1],
        cash_received=pos.cash_received,
    )


def _repriced(row: HoldingRow, price: float, source: str) -> HoldingRow:
    value = row.shares * price
    return replace(
        row,
        close=price,
        value=value,
        ret=(value + row.cash_received) / row.cost - 1,
        pnl=value + row.cash_received - row.cost,
        price_source=source,
    )


def apply_quotes(rows: Sequence[HoldingRow], quotes: Mapping[str, LiveQuote]) -> LiveHoldings:
    """把盘中报价覆盖到持仓上，只用于显示：现价、市值、盈亏、收益按报价重算，其余不变。

    报价的现价大于 0 用现价；现价为 0（停牌或还没成交）且有昨收时用昨收并标为停牌；
    没有报价的保持收盘价。
    """
    out: list[HoldingRow] = []
    times: list[datetime] = []
    live = suspended = 0
    for row in rows:
        quote = quotes.get(row.symbol)
        if quote is not None and quote.price > 0:
            out.append(_repriced(row, quote.price, "live"))
            times.append(quote.at)
            live += 1
        elif quote is not None and quote.prev_close > 0:
            out.append(_repriced(row, quote.prev_close, "suspended"))
            times.append(quote.at)
            suspended += 1
        else:
            out.append(row)
    return LiveHoldings(
        rows=out,
        quote_time=max(times, default=None),
        live_count=live,
        suspended_count=suspended,
        close_count=len(out) - live - suspended,
    )


def _entry_row(row: LedgerEntry, voided: set[int], names: Names | None, writable: bool) -> EntryRow:
    if row.kind in ("deposit", "withdraw"):
        amount = row.price
    elif row.kind == "void":
        amount = 0.0
    else:
        amount = row.shares * row.price
    is_voided = row.id in voided
    return EntryRow(
        id=row.id,
        kind=row.kind,
        kind_label=KIND_LABELS.get(row.kind, row.kind),
        day=row.trade_day,
        effective_day=row.effective_day,
        symbol=row.symbol,
        name=name_of(names, row.symbol),
        shares=row.shares,
        price=row.price,
        amount=amount,
        fees=row.fees,
        fees_estimated=row.fees_estimated,
        origin=row.origin,
        signal_id=row.signal_id,
        voids_entry_id=row.voids_entry_id,
        note=row.note,
        voided=is_voided,
        can_void=writable and row.kind != "void" and not is_voided,
    )


def _action_rows(action: CorporateAction, names: Names | None) -> list[EntryRow]:
    """推算出的分红、送转（各一条，没有的不列）。"""

    def derived(kind: str, shares: float, amount: float) -> EntryRow:
        return EntryRow(
            id=None,
            kind=kind,
            kind_label=KIND_LABELS[kind],
            day=action.day,
            effective_day=action.day,
            symbol=action.symbol,
            name=name_of(names, action.symbol),
            shares=shares,
            price=0.0,
            amount=amount,
            fees=0.0,
            fees_estimated=False,
            origin=None,
            signal_id=None,
            voids_entry_id=None,
            note="",
            voided=False,
            can_void=False,
        )

    rows: list[EntryRow] = []
    if action.cash > 0:
        rows.append(derived("dividend", 0.0, action.cash))
    if action.bonus_shares > 0:
        rows.append(derived("bonus", action.bonus_shares, 0.0))
    return rows


def _manual_entries(
    rows: Sequence[LedgerEntry],
    actions: Sequence[CorporateAction],
    names: Names | None,
    writable: bool,
) -> list[EntryRow]:
    """流水与推算出的分红送转：按日期排序，同日先实际流水（按录入先后）后推算记录。"""
    voided = {r.voids_entry_id for r in rows if r.kind == "void" and r.voids_entry_id is not None}
    items: list[tuple[date, int, int, EntryRow]] = [
        (r.trade_day, 0, i, _entry_row(r, voided, names, writable)) for i, r in enumerate(rows)
    ]
    for j, action in enumerate(actions):
        items.extend(
            (action.day, 1, j * 2 + k, row) for k, row in enumerate(_action_rows(action, names))
        )
    items.sort(key=lambda it: it[:3])
    return [row for *_, row in items]


def manual_through(rows: Sequence[LedgerEntry], calendar: TradingCalendar, today: date) -> date:
    """手动账查看到哪一天：不晚于今天的最近交易日；有更晚才进入净值的转入时取到那一天。"""
    days = calendar.between(calendar.first_day, today)
    if not days:
        raise ValidationError(f"交易日历里没有 {today} 及以前的交易日")
    return max([days[-1], *(r.effective_day for r in rows if r.kind != "void")])


def manual_book(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: Market,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    names: Names | None = None,
) -> ManualBookView:
    """手动账：按全部流水重放（manual_ops.manual_state），持仓按查看日的收盘价计值。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    book = repo.get_book_of_kind(s, user_id, sub.id, "manual")
    rows = repo.list_ledger_entries(s, user_id, book.id)
    through = manual_through(rows, calendar, today)
    state = manual_ops.manual_state(s, user_id, sub.id, market, calendar, cfg, through)
    nav = state.equity[-1] if state.equity else sub.initial_cash
    unit_nav = state.unit_nav[-1] if state.unit_nav else 1.0
    cash = state.cash
    summary = BookSummary(nav, unit_nav, cash, nav - cash, len(state.positions), through)
    strategy: StrategyConfig = subscription_config(cfg, sub).strategy
    holdings = [
        _holding(
            pos,
            state.origins.get(pos.symbol, "self"),
            market,
            through,
            names,
            exit_levels(pos, strategy),
        )
        for pos in state.positions.values()
    ]
    writable = sub.status != "closed"
    return ManualBookView(
        subscription=subscription_view(sub),
        summary=summary,
        holdings=holdings,
        entries=_manual_entries(rows, state.actions, names, writable),
    )


def manual_page(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: Market,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    names: Names | None = None,
) -> ManualBookPage:
    """手动账页面：同 manual_book；流水不一致时改为带原因的结果，并照常列出原始流水。"""
    try:
        view = manual_book(s, user_id, subscription_id, market, calendar, cfg, today, names=names)
    except LedgerInconsistentError as exc:
        sub = repo.get_subscription(s, user_id, subscription_id)
        book = repo.get_book_of_kind(s, user_id, sub.id, "manual")
        rows = repo.list_ledger_entries(s, user_id, book.id)
        entries = _manual_entries(rows, [], names, sub.status != "closed")
        return ManualBookPage(subscription_view(sub), None, [], entries, exc.reason)
    return ManualBookPage(view.subscription, view.summary, view.holdings, view.entries, None)


def manual_notice(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: Market,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
) -> str | None:
    """手动账流水不一致时的原因；一致则为 None。"""
    return manual_page(s, user_id, subscription_id, market, calendar, cfg, today).error


def system_book(
    s: Session,
    user_id: int,
    subscription_id: int,
    market: Market | None = None,
    *,
    names: Names | None = None,
) -> SystemBookView:
    """系统账：每晚推进后保存的状态与每日净值。给了 market 时持仓按最近一次推进日的收盘价计值。"""
    sub = repo.get_subscription(s, user_id, subscription_id)
    book = repo.get_book_of_kind(s, user_id, sub.id, "system")
    days = repo.list_book_days(s, user_id, book.id)
    rows = repo.list_ledger_entries(s, user_id, book.id)
    entries = [_entry_row(r, set(), names, False) for r in rows]
    nav = [NavPoint(d.day, d.equity, d.cash, d.unit_nav) for d in days]
    state_row = repo.get_system_state(s, user_id, book.id)
    if state_row is None or not days:
        return SystemBookView(subscription_view(sub), None, [], entries, nav)
    positions = load_state(state_row.state)[0].positions
    last = days[-1]
    summary = BookSummary(
        last.equity, last.unit_nav, last.cash, last.equity - last.cash, len(positions), last.day
    )
    holdings = [
        _holding(pos, "signal", market, last.day, names, (None, None)) for pos in positions.values()
    ]
    return SystemBookView(subscription_view(sub), summary, holdings, entries, nav)


def book_symbols(s: Session, user_id: int, subscription_id: int) -> frozenset[str]:
    """该订阅各页面的行情需要覆盖的股票：手动账流水里出现过的、系统账当前持仓、手动账未结信号。

    网页进程按这个集合取行情（全量面板太大，不能在网页进程里建）。
    """
    sub = repo.get_subscription(s, user_id, subscription_id)
    manual = repo.get_book_of_kind(s, user_id, sub.id, "manual")
    system = repo.get_book_of_kind(s, user_id, sub.id, "system")
    symbols = {r.symbol for r in repo.list_ledger_entries(s, user_id, manual.id) if r.symbol}
    state_row = repo.get_system_state(s, user_id, system.id)
    if state_row is not None:
        symbols |= set(load_state(state_row.state)[0].positions)
    symbols |= {sig.symbol for sig in repo.list_signals(s, user_id, manual.id, statuses=_OPEN)}
    return frozenset(symbols)
