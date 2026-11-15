"""手动账重放与流水校验（纯函数，用内存里的假行情）。"""

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta

import pytest

from qt.calendar import TradingCalendar
from qt.costs import CostModel
from qt.models import ExitReason
from qt.risk import RiskEventKind
from qt.service.errors import LedgerInconsistentError, ValidationError
from qt.service.manual_book import (
    CorporateAction,
    Entry,
    EntryKind,
    ManualState,
    replay,
    validate_entry,
)
from tests.fakes import default_costs, default_risk, weekdays

DAYS = weekdays(date(2021, 1, 4), 1000)
CAL = TradingCalendar(DAYS)
RISK = default_risk()
COSTS = CostModel(default_costs())
B = 900  # 账本起始日在 DAYS 中的下标
D1, D2, D3 = DAYS[B], DAYS[B + 1], DAYS[B + 2]
START = 100_000.0
S1, S2 = "600001", "600002"


class FakeMarket:
    """价格为阶梯函数：set_price 之后（含当日）一直是该价格；未设置时为 10.00。"""

    def __init__(self) -> None:
        self._changes: dict[str, list[tuple[date, float]]] = {}
        self._dividends: dict[str, list[tuple[date, float, float]]] = {}
        self._delisted: dict[str, date] = {}

    def set_price(self, symbol: str, day: date, price: float) -> None:
        changes = self._changes.setdefault(symbol, [])
        changes.append((day, price))
        changes.sort()

    def add_dividend(self, symbol: str, ex_day: date, cash: float, bonus: float = 0.0) -> None:
        self._dividends.setdefault(symbol, []).append((ex_day, cash, bonus))

    def delist(self, symbol: str, day: date) -> None:
        self._delisted[symbol] = day

    def last_close(self, day: date, symbol: str) -> float:
        changes = self._changes.get(symbol, [])
        i = bisect_right([c[0] for c in changes], day)
        return changes[i - 1][1] if i > 0 else 10.0

    def dividends(self, symbol: str) -> Sequence[tuple[date, float, float]]:
        return self._dividends.get(symbol, [])

    def delisted_before(self, day: date, symbol: str) -> bool:
        delisted = self._delisted.get(symbol)
        return delisted is not None and delisted < day

    def industry(self, symbol: str) -> str:
        return "银行"


def _entry(
    id_: int,
    kind: EntryKind,
    day: date,
    symbol: str | None = None,
    shares: float = 0.0,
    price: float = 0.0,
    fees: float = 0.0,
    *,
    effective_day: date | None = None,
    origin: str | None = None,
    exit_reason: ExitReason | None = None,
    voids: int | None = None,
) -> Entry:
    return Entry(
        id=id_,
        kind=kind,
        trade_day=day,
        effective_day=effective_day or day,
        symbol=symbol,
        shares=shares,
        price=price,
        fees=fees,
        origin=origin,
        exit_reason=exit_reason,
        voids_entry_id=voids,
        industry="银行",
        entry_metrics={"dividend_yield": 0.05} if symbol else {},
    )


def buy(
    id_: int, day: date, shares: float, price: float, fees: float = 5.0, symbol: str = S1
) -> Entry:
    return _entry(id_, EntryKind.BUY, day, symbol, shares, price, fees, origin="signal")


def sell(
    id_: int,
    day: date,
    shares: float,
    price: float,
    fees: float = 5.0,
    reason: ExitReason | None = None,
    symbol: str = S1,
) -> Entry:
    return _entry(id_, EntryKind.SELL, day, symbol, shares, price, fees, exit_reason=reason)


def deposit(id_: int, day: date, amount: float) -> Entry:
    return _entry(id_, EntryKind.DEPOSIT, day, price=amount)


def withdraw(id_: int, day: date, amount: float) -> Entry:
    return _entry(id_, EntryKind.WITHDRAW, day, price=amount)


def transfer(
    id_: int, bought: date, effective: date, shares: float, cost_price: float, symbol: str = S2
) -> Entry:
    return _entry(
        id_,
        EntryKind.TRANSFER_IN,
        bought,
        symbol,
        shares,
        cost_price,
        effective_day=effective,
        origin="self",
    )


def void(id_: int, day: date, target: int) -> Entry:
    return _entry(id_, EntryKind.VOID, day, voids=target)


def run(
    entries: Sequence[Entry],
    market: FakeMarket,
    through: date,
    start_day: date = D1,
    start_cash: float = START,
) -> ManualState:
    return replay(entries, market, CAL, RISK, COSTS, start_cash, start_day, through)


# ---------------------------------------------------------------- 重放


def test_buy_then_hold_values_position_and_unit_nav() -> None:
    m = FakeMarket()
    m.set_price(S1, D2, 11.0)
    st = run([buy(1, D1, 1000, 10.0)], m, D2)
    assert st.cash == pytest.approx(89_995.0)
    assert st.equity[-1] == pytest.approx(100_995.0)
    assert st.unit_nav[-1] == pytest.approx(1.00995)
    assert st.positions[S1].days_held == 2
    assert st.positions[S1].cost == pytest.approx(10_005.0)
    assert st.origins[S1] == "signal"
    assert st.days == [D1, D2]
    assert st.units == pytest.approx(START)


def test_deposit_and_withdraw_do_not_move_unit_nav() -> None:
    # 价格只在 D2 变动：入金、出金当日的净值与不入金时相同；此后现金占比不同，
    # 价格再变动时净值自然不同，所以 D3 价格不变，逐日都应相同
    m = FakeMarket()
    m.set_price(S1, D2, 11.0)
    base = run([buy(1, D1, 1000, 10.0)], m, D3)
    entries = [buy(1, D1, 1000, 10.0), deposit(2, D2, 50_000.0), withdraw(3, D3, 20_000.0)]
    flows = run(entries, m, D3)
    assert flows.unit_nav == pytest.approx(base.unit_nav)
    assert flows.unit_nav[1] == pytest.approx(100_995.0 / START)
    expected_units = START + 50_000.0 / base.unit_nav[1] - 20_000.0 / base.unit_nav[2]
    assert flows.units == pytest.approx(expected_units)
    assert flows.cash == pytest.approx(base.cash + 30_000.0)
    assert flows.equity[-1] == pytest.approx(base.equity[-1] + 30_000.0)


def test_deposit_day_unit_nav_matches_no_deposit_even_when_price_moves() -> None:
    m = FakeMarket()
    m.set_price(S1, D2, 11.0)
    m.set_price(S1, D3, 12.0)
    base = run([buy(1, D1, 1000, 10.0)], m, D3)
    flows = run([buy(1, D1, 1000, 10.0), deposit(2, D3, 50_000.0)], m, D3)
    assert flows.unit_nav == pytest.approx(base.unit_nav)
    assert flows.units == pytest.approx(START + 50_000.0 / base.unit_nav[2])


def test_transfer_in_adds_units_at_market_value_and_keeps_user_cost() -> None:
    m = FakeMarket()
    st = run([transfer(1, DAYS[B - 5], D2, 1000, 8.0)], m, D2)
    nav_d = st.unit_nav[-1]
    assert nav_d == pytest.approx(1.0)
    assert st.cash == pytest.approx(100_000.0)  # 不扣现金
    assert st.units == pytest.approx(100_000.0 + 10_000.0 / nav_d)
    assert st.positions[S2].ret(10.0) == pytest.approx(0.25)
    assert st.positions[S2].entry_day == DAYS[B - 5]
    assert st.origins[S2] == "self"
    assert st.equity[-1] == pytest.approx(110_000.0)


def test_transfer_in_backfills_days_held_and_peak_return() -> None:
    m = FakeMarket()
    bought = DAYS[B - 30]
    m.set_price(S2, bought, 8.0)
    m.set_price(S2, DAYS[B - 10], 12.0)  # 期间最高收盘 12.00
    m.set_price(S2, DAYS[B - 9], 10.0)
    st = run([transfer(1, bought, D1, 1000, 8.0)], m, D1)
    pos = st.positions[S2]
    assert pos.days_held == 31
    assert pos.peak_return == pytest.approx(0.5)
    assert pos.cash_received == 0.0


def test_transfer_in_days_held_keeps_counting_after_effective_day() -> None:
    m = FakeMarket()
    st = run([transfer(1, DAYS[B - 30], D1, 1000, 8.0)], m, D3)
    assert st.positions[S2].days_held == 33


def test_transfer_in_peak_return_backfill_excludes_pre_transfer_cash_dividends() -> None:
    m = FakeMarket()
    bought = DAYS[B - 10]
    m.set_price(S2, bought, 8.0)
    m.add_dividend(S2, DAYS[B - 5], 1.0)
    m.set_price(S2, DAYS[B - 3], 10.0)
    st = run([transfer(1, bought, D1, 1000, 8.0)], m, D1)
    # 只看价格：10.00 / 8.00 - 1；转入前的现金分红不计（与 cash_received 从 0 开始一致）
    assert st.positions[S2].peak_return == pytest.approx(0.25)
    assert st.positions[S2].ret(10.0) == pytest.approx(st.positions[S2].peak_return)
    assert st.cash == pytest.approx(START)  # 转入之前的分红不进这本账的现金


def test_transfer_in_backfill_treats_entered_shares_as_post_bonus_holding() -> None:
    m = FakeMarket()
    bought = DAYS[B - 10]
    m.set_price(S2, bought, 12.0)  # 送转前 1000 股 * 12.00 = 12000，收益 0
    m.add_dividend(S2, DAYS[B - 5], 0.0, 0.5)
    m.set_price(S2, DAYS[B - 5], 10.0)  # 送转后 1500 股 * 10.00 = 15000
    st = run([transfer(1, bought, D1, 1500, 8.0)], m, D1)
    assert st.positions[S2].shares == 1500
    assert st.positions[S2].peak_return == pytest.approx(15_000.0 / 12_000.0 - 1)


def test_full_withdrawal_then_deposit_keeps_unit_nav() -> None:
    m = FakeMarket()
    entries = [withdraw(1, D1, START), deposit(2, D3, 20_000.0)]
    st = run(entries, m, D3)
    assert st.unit_nav == pytest.approx([1.0, 1.0, 1.0])
    assert st.units == pytest.approx(20_000.0)


def test_cash_dividend_goes_to_cash_and_counts_in_return() -> None:
    m = FakeMarket()
    m.add_dividend(S1, D2, 0.5)
    m.set_price(S1, D2, 9.5)
    st = run([buy(1, D1, 1000, 10.0)], m, D2)
    pos = st.positions[S1]
    assert st.cash == pytest.approx(START - 10_005.0 + 500.0)
    assert pos.cash_received == pytest.approx(500.0)
    assert pos.ret(9.5) == pytest.approx((9_500.0 + 500.0) / 10_005.0 - 1)
    assert st.actions == [CorporateAction(D2, S1, 500.0, 0.0)]


def test_bonus_shares_increase_shares() -> None:
    m = FakeMarket()
    m.add_dividend(S1, D2, 0.0, 0.5)
    st = run([buy(1, D1, 1000, 10.0)], m, D2)
    assert st.positions[S1].shares == pytest.approx(1500.0)
    assert st.cash == pytest.approx(START - 10_005.0)
    assert st.actions == [CorporateAction(D2, S1, 0.0, 500.0)]


def test_dividend_applies_only_to_shares_held_before_ex_date() -> None:
    m = FakeMarket()
    m.add_dividend(S1, D2, 0.5)
    st = run([buy(1, D1, 1000, 10.0), buy(2, D2, 500, 10.0)], m, D2)
    assert st.positions[S1].shares == 1500.0
    assert st.positions[S1].cash_received == pytest.approx(500.0)
    assert st.cash == pytest.approx(START - 10_005.0 - 5_005.0 + 500.0)

    only_on_ex_day = run([buy(1, D2, 1000, 10.0)], m, D2)
    assert only_on_ex_day.actions == []
    assert only_on_ex_day.positions[S1].cash_received == 0.0


def test_partial_sell_scales_cost_and_emits_trade() -> None:
    m = FakeMarket()
    m.add_dividend(S1, D2, 0.5)
    st = run([buy(1, D1, 1000, 10.0), sell(2, D3, 400, 11.0)], m, D3)
    pos = st.positions[S1]
    assert pos.shares == 600.0
    assert pos.cost == pytest.approx(10_005.0 * 0.6)
    assert pos.cash_received == pytest.approx(500.0 * 0.6)
    assert len(st.trades) == 1
    t = st.trades[0]
    assert t.shares == 400
    assert t.cost == pytest.approx(10_005.0 * 0.4)
    assert t.proceeds == pytest.approx(4_395.0)
    assert t.cash_received == pytest.approx(200.0)
    assert (t.entry_day, t.exit_day, t.holding_days) == (D1, D3, 2)
    assert t.fill_price == 11.0
    assert t.exit_reason is None and t.origin == "signal"
    assert st.cash == pytest.approx(START - 10_005.0 + 500.0 + 4_395.0)
    assert st.risk.exits == ()


def test_full_sell_with_stop_loss_reason_starts_cooldown() -> None:
    m = FakeMarket()
    st = run([buy(1, D1, 1000, 10.0), sell(2, D2, 1000, 8.0, reason=ExitReason.STOP_LOSS)], m, D2)
    assert st.positions == {} and st.origins == {}
    assert (S1, D2) in st.risk.exits
    assert st.trades[0].exit_reason is ExitReason.STOP_LOSS

    no_reason = run([buy(1, D1, 1000, 10.0), sell(2, D2, 1000, 8.0)], m, D2)
    assert no_reason.risk.exits == ()
    assert len(no_reason.trades) == 1


def test_voided_entry_and_its_void_are_ignored() -> None:
    m = FakeMarket()
    st = run([buy(1, D1, 1000, 10.0), void(2, D2, 1)], m, D2)
    assert st.positions == {}
    assert st.cash == pytest.approx(START)


def test_backdated_entry_replays_dividend_between_trade_day_and_today() -> None:
    m = FakeMarket()
    today = DAYS[B + 5]
    m.add_dividend(S1, DAYS[B + 2], 0.3)
    # 先录了今天的入金，之后才补录 5 个交易日前的买入（id 更大）
    entries = [deposit(1, today, 1_000.0), buy(2, D1, 1000, 10.0)]
    st = run(entries, m, today)
    assert st.positions[S1].cash_received == pytest.approx(300.0)
    assert st.cash == pytest.approx(START + 1_000.0 - 10_005.0 + 300.0)
    assert st.actions == [CorporateAction(DAYS[B + 2], S1, 300.0, 0.0)]


def test_drawdown_pause_event_from_unit_nav_not_from_withdrawal() -> None:
    m = FakeMarket()
    entries = [buy(1, D1, 6000, 10.0, fees=0.0), withdraw(2, D2, 30_000.0)]
    st = run(entries, m, D2)
    assert st.risk_events == []
    assert st.unit_nav[-1] == pytest.approx(1.0)

    m.set_price(S1, D3, 6.5)  # 10000 现金 + 6000 * 6.5 = 49000，份额 70000 → 0.7
    st = run(entries, m, D3)
    assert st.unit_nav[-1] == pytest.approx(0.7)
    assert [e.kind for e in st.risk_events] == [RiskEventKind.PAUSE]
    assert st.risk.paused


def test_replay_rejects_ledger_that_oversells() -> None:
    with pytest.raises(ValueError, match="超过持仓"):
        run([buy(1, D1, 100, 10.0), sell(2, D2, 200, 10.0)], FakeMarket(), D2)


# ---------------------------------------------------------------- 校验

TODAY = DAYS[B + 40]
KNOWN = frozenset({S1, S2})
LEDGER = [
    buy(1, DAYS[B + 30], 1000, 10.0),
    sell(2, DAYS[B + 35], 1000, 11.0),
    deposit(3, DAYS[B + 31], 1_000.0),
    void(4, DAYS[B + 32], 3),
]


def _saturday_before(day: date) -> date:
    d = day
    while d.weekday() != 5:
        d -= timedelta(days=1)
    return d


# 周六录入：转入的生效日为下周一
WEEKEND = DAYS[B + 44] + timedelta(days=1)
NEXT = DAYS[B + 45]


def _check(
    new: Entry,
    market: FakeMarket | None = None,
    ledger: Sequence[Entry] = tuple(LEDGER),
    today: date = TODAY,
) -> None:
    validate_entry(
        ledger,
        new,
        market or FakeMarket(),
        CAL,
        RISK,
        COSTS,
        START,
        D1,
        today=today,
        backdate_limit_days=10,
        known_symbols=KNOWN,
    )


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (sell(9, DAYS[B + 32], 1500, 10.0), "超过持仓"),
        (buy(9, _saturday_before(TODAY), 100, 10.0), "不是交易日"),
        (buy(9, DAYS[B + 41], 100, 10.0), "不能晚于今天"),
        (buy(9, DAYS[B + 29], 100, 10.0), "最多可以早于今天 10 个交易日"),
        (buy(9, TODAY, 150, 10.0), "100 的整数倍"),
        (buy(9, TODAY, 100, 10.0, symbol="999999"), "代码不存在"),
        (withdraw(9, TODAY, 200_000.0), "超过现金"),
        (void(9, TODAY, 3), "已作废"),
        (void(9, TODAY, 1), "作废后持仓为负"),
        (buy(9, TODAY, 100, 0.0), "价格必须为正"),
        (sell(9, TODAY, 0, 10.0), "股数必须为正"),
        (buy(9, TODAY, 100, 10.0, fees=-1.0), "费用不能为负"),
        (void(9, TODAY, 4), "不能作废一条作废记录"),
        (void(9, TODAY, 99), "不存在"),
        (sell(9, DAYS[B + 33], 600, 11.0), "超过持仓"),  # 当日够卖，但让之后那笔卖出超卖
        (buy(9, TODAY, 100_000, 10.0), "超过现金"),
        (transfer(9, DAYS[B + 41], DAYS[B + 41], 1000, 8.0), "不能晚于今天"),
        (transfer(9, DAYS[B - 3], DAYS[B + 42], 1000, 8.0), "生效日"),
        (transfer(9, DAYS[B - 3], DAYS[B + 33], 1000, 8.0, symbol=S1), "已在持仓中"),
    ],
)
def test_validate_entry_rejects(bad: Entry, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _check(bad)


def test_validate_entry_rejects_buying_delisted_stock() -> None:
    m = FakeMarket()
    m.delist(S2, DAYS[B + 20])
    with pytest.raises(ValidationError, match="已退市"):
        _check(buy(9, TODAY, 100, 10.0, symbol=S2), m)


def test_validate_entry_rejects_transfer_effective_before_trade_day() -> None:
    with pytest.raises(ValidationError, match="生效日"):
        _check(transfer(9, DAYS[B + 39], DAYS[B + 38], 1000, 8.0))


def test_validate_entry_accepts_valid_entries() -> None:
    _check(buy(9, DAYS[B + 30], 100, 10.0))  # 恰好早于今天 10 个交易日
    only_buy = LEDGER[:1]
    _check(sell(9, DAYS[B + 33], 400, 11.0), ledger=only_buy)  # 部分卖出
    _check(sell(9, DAYS[B + 33], 33, 11.0), ledger=only_buy)  # 卖出不要求整手
    _check(withdraw(9, TODAY, 1_000.0))
    _check(void(9, TODAY, 2))  # 作废卖出：持仓恢复，不出现负数


def test_transfer_in_may_have_any_past_buy_date() -> None:
    # 3 年前买入，周六录入、下周一生效
    _check(transfer(9, DAYS[B - 750], NEXT, 1000, 8.0), today=WEEKEND)
    _check(transfer(9, DAYS[B - 750], TODAY, 1000, 8.0))


def test_transfer_in_rejected_when_symbol_held_at_future_effective_day() -> None:
    ledger = [*LEDGER, buy(5, DAYS[B + 44], 100, 10.0, symbol=S2)]
    with pytest.raises(ValidationError, match="已在持仓中"):
        _check(transfer(9, DAYS[B - 3], NEXT, 1000, 8.0), ledger=ledger, today=WEEKEND)


def test_buy_rejected_when_pending_future_transfer_of_same_symbol() -> None:
    ledger = [*LEDGER, transfer(5, DAYS[B - 3], NEXT, 1000, 8.0)]
    friday = DAYS[B + 44]
    _check(buy(9, friday, 100, 10.0), ledger=ledger, today=WEEKEND)  # 其他股票不受影响
    with pytest.raises(ValidationError, match="已在持仓中"):
        _check(buy(9, friday, 100, 10.0, symbol=S2), ledger=ledger, today=WEEKEND)


def test_transfer_in_delisting_checked_at_effective_day() -> None:
    m = FakeMarket()
    m.delist(S2, DAYS[B - 50])  # 买入在退市之前，但生效日已退市
    with pytest.raises(ValidationError, match="已退市"):
        _check(transfer(9, DAYS[B - 100], TODAY, 1000, 8.0), m)


def test_transfer_in_buy_date_before_calendar_start_names_earliest_date() -> None:
    with pytest.raises(ValidationError, match=f"最早可填 {DAYS[0]}"):
        _check(transfer(9, DAYS[0] - timedelta(days=7), TODAY, 1000, 8.0))


def test_inconsistent_existing_ledger_is_not_blamed_on_new_entry() -> None:
    broken = [buy(1, DAYS[B + 30], 100, 10.0), sell(2, DAYS[B + 31], 200, 10.0)]
    with pytest.raises(LedgerInconsistentError, match="现有流水"):
        _check(deposit(9, TODAY, 1_000.0), ledger=broken)


def _revised_dividend_ledger() -> tuple[FakeMarket, list[Entry]]:
    """S1 的一笔现金分红（2700 元）已被花掉之后，数据源修订删掉了它：现有流水重放出负现金。"""
    original = FakeMarket()
    revised = FakeMarket()
    for m in (original, revised):
        m.add_dividend(S2, DAYS[B + 34], 0.2)
    original.add_dividend(S1, DAYS[B + 31], 0.3)
    ledger = [
        buy(1, DAYS[B + 30], 9000, 10.0),
        sell(2, DAYS[B + 32], 9000, 10.0),
        buy(3, DAYS[B + 33], 9900, 10.0, symbol=S2),
        withdraw(4, DAYS[B + 35], 3_000.0),
    ]
    run(ledger, original, TODAY)  # 修订前一致
    with pytest.raises(ValueError, match="超过现金"):
        run(ledger, revised, TODAY)
    return revised, ledger


def test_deposit_repairs_ledger_broken_by_revised_dividend() -> None:
    market, ledger = _revised_dividend_ledger()
    _check(deposit(9, DAYS[B + 35], 5_000.0), market, ledger)  # 不抛：加上它之后能重放
    state = run([*ledger, deposit(9, DAYS[B + 35], 5_000.0)], market, TODAY)
    assert state.cash >= 0


def test_void_repairs_ledger_broken_by_revised_dividend() -> None:
    market, ledger = _revised_dividend_ledger()
    _check(void(9, TODAY, 4), market, ledger)


def test_unrelated_entry_on_still_broken_ledger_is_rejected() -> None:
    market, ledger = _revised_dividend_ledger()
    with pytest.raises(LedgerInconsistentError, match=r"入金.*作废"):
        _check(buy(9, TODAY, 100, 10.0), market, ledger)
