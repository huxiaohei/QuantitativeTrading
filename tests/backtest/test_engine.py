"""回测引擎测试：用真实的 populate_store + build_panel 构造面板，桩策略精确控制信号。

约定：D 是连续工作日（周一至周五，同时作为交易日历）；D[0] 给出买入信号，
D[1] 开盘成交。除非特别说明，价格恒为 10 元，买单被风控定为 1000 股
（初始资金 11 万，单只上限 10%，一手约 1001 元）。
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from qt.backtest.engine import BacktestResult, limit_prices, run_backtest
from qt.calendar import TradingCalendar
from qt.config import CostsConfig, StampTaxPeriod
from qt.costs import CostModel
from qt.data.store import ParquetStore
from qt.features import build_panel
from qt.models import BuyCandidate, ExitReason, Position, SellSignal
from qt.risk import RiskEventKind, RiskManager
from qt.strategy.base import Strategy
from qt.strategy.high_dividend_low import HighDividendLow
from tests.fakes import (
    ScriptedStrategy,
    default_app_config,
    default_costs,
    default_risk,
    default_strategy_config,
    default_universe_config,
    make_candidate,
    make_ohlc_bars,
    make_securities,
    make_st_history,
    populate_store,
    weekdays,
)

D = weekdays(date(2024, 1, 2), 30)
S = "600000"
S2 = "600001"
N = 12  # 默认回测 D[0]..D[N-1]
COSTS = CostModel(default_costs())
CASH = 110_000.0


def _bars(
    symbol: str,
    opens: Mapping[int, float] | None = None,
    closes: Mapping[int, float] | None = None,
    *,
    skip: Sequence[int] = (),
    adj: Mapping[int, float] | None = None,
    n: int = N,
) -> pd.DataFrame:
    """构造日线：开收盘默认 10；opens/closes 按日序号覆盖（收盘默认同开盘）；skip 为停牌日序号。"""
    opens = opens or {}
    closes = closes or {}
    adj = adj or {}
    idx = [i for i in range(n) if i not in skip]
    open_ = [opens.get(i, 10.0) for i in idx]
    close = [closes.get(i, opens.get(i, 10.0)) for i in idx]
    factors = [adj.get(i, 1.0) for i in idx]
    return make_ohlc_bars(symbol, [D[i] for i in idx], open_, close, factors)


def _run(
    tmp_path: Path,
    bars: Sequence[pd.DataFrame],
    strategy: Strategy,
    *,
    delist: Mapping[str, date] | None = None,
    st_history: pd.DataFrame | None = None,
    risk_update: Mapping[str, float] | None = None,
    initial_cash: float = CASH,
    name: str = "store",
    costs_cfg: CostsConfig | None = None,
) -> BacktestResult:
    cfg = default_app_config()
    calendar = TradingCalendar(D)
    store = ParquetStore(tmp_path / name)
    delist = delist or {}
    symbols = [str(b["symbol"].iloc[0]) for b in bars]
    populate_store(
        store,
        securities=make_securities(
            [(s, "sh_main", date(2010, 1, 1), delist.get(s)) for s in symbols]
        ),
        bars=bars,
        st_history=st_history,
    )
    panel = build_panel(store, calendar, cfg, D[0], D[N - 1])
    risk_cfg = default_risk().model_copy(update=dict(risk_update or {}))
    costs = CostModel(costs_cfg or cfg.costs)
    risk = RiskManager(risk_cfg, calendar, costs)
    return run_backtest(panel, strategy, risk, costs, calendar, D[0], D[N - 1], initial_cash)


def _buy(*symbols: str, day: int = 0, close: float = 10.0) -> dict[date, list[BuyCandidate]]:
    return {D[day]: [make_candidate(s, close=close, day=D[day]) for s in symbols]}


def _at(series: "pd.Series[float]", day: date) -> float:
    return float(cast(float, series.at[day]))


# ---------- 买入撮合 ----------


def test_signal_on_t_fills_at_t_plus_1_open(tmp_path: Path) -> None:
    result = _run(tmp_path, [_bars(S, {1: 10.2})], ScriptedStrategy(buys=_buy(S)), name="a")
    assert _at(result.equity, D[0]) == pytest.approx(CASH)  # 信号当天收盘无持仓
    assert _at(result.cash, D[0]) == CASH
    assert result.trades == []
    (pos,) = result.open_positions
    assert pos.entry_day == D[1]
    assert pos.shares == 1000
    assert pos.cost == pytest.approx(COSTS.buy_cost(10.2, 1000))
    assert pos.industry == "银行"


def test_buy_blocked_at_limit_up_then_filled_next_day(tmp_path: Path) -> None:
    # D[1] 前收 10，涨停价 11.00：开盘 11.00 买不到；D[2] 开盘 10.5 成交
    bars = _bars(S, {1: 11.0, 2: 10.5})
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)))
    (pos,) = result.open_positions
    assert pos.entry_day == D[2]
    assert pos.cost == pytest.approx(COSTS.buy_cost(10.5, 1000))
    assert result.expired_buys == []


def test_buy_order_expires_after_3_days(tmp_path: Path) -> None:
    # 连续三个交易日开盘涨停（11.0、12.1、13.31），第 3 次尝试后作废；第 4 天不再成交
    bars = _bars(S, {1: 11.0, 2: 12.1, 3: 13.31, 4: 13.0})
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)))
    assert result.open_positions == []
    assert result.trades == []
    (order,) = result.expired_buys
    assert order.symbol == S
    assert order.signal_day == D[0]


def test_buy_expires_when_suspended_for_three_days(tmp_path: Path) -> None:
    result = _run(tmp_path, [_bars(S, skip=[1, 2, 3])], ScriptedStrategy(buys=_buy(S)))
    assert len(result.expired_buys) == 1
    assert result.open_positions == []


def test_buy_trimmed_to_affordable_lots_when_cash_short(tmp_path: Path) -> None:
    # 信号日按收盘价 10 定仓：A 5 手、B 4 手；次日开盘 10.99（未涨停），
    # A 成交后现金只够 B 买 3 手
    risk_update = {"max_position_pct": 0.6, "max_industry_pct": 1.0}
    bars = [_bars(S, {1: 10.99}), _bars(S2, {1: 10.99})]
    result = _run(
        tmp_path,
        bars,
        ScriptedStrategy(buys=_buy(S, S2)),
        risk_update=risk_update,
        initial_cash=9600.0,
    )
    shares = {p.symbol: p.shares for p in result.open_positions}
    assert shares == {S: 500, S2: 300}
    spent = COSTS.buy_cost(10.99, 500) + COSTS.buy_cost(10.99, 300)
    assert _at(result.cash, D[1]) == pytest.approx(9600.0 - spent)


def test_costs_deducted_from_cash(tmp_path: Path) -> None:
    result = _run(tmp_path, [_bars(S)], ScriptedStrategy(buys=_buy(S)))
    assert _at(result.cash, D[1]) == pytest.approx(CASH - COSTS.buy_cost(10.0, 1000))
    assert _at(result.cash, D[1]) == pytest.approx(CASH - 10015.1001)


# ---------- 卖出撮合 ----------


def test_sell_blocked_at_limit_down_carries_over(tmp_path: Path) -> None:
    # D[2] 收盘后发出卖出信号；D[3]、D[4] 开盘跌停（9.0、8.1）顺延；D[5] 成交
    bars = _bars(S, {3: 9.0, 4: 8.1, 5: 8.0})
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[2]: [(S, ExitReason.STOP_LOSS)]})
    result = _run(tmp_path, [bars], strategy)
    (trade,) = result.trades
    assert trade.exit_day == D[5]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(8.0, 1000, trade.exit_day))
    assert result.open_positions == []


def test_st_position_uses_5_percent_limit(tmp_path: Path) -> None:
    # D[2] 起变为 ST。D[3] 前收 10.5，跌停价 10.5*0.95=9.975 逢五进一为 9.98
    # （银行家舍入会得到 9.97，从而误判可成交）；开盘 9.98 顺延，D[4] 成交
    bars = _bars(S, {0: 10.5, 1: 10.5, 2: 10.5, 3: 9.98, 4: 10.0})
    st = make_st_history([(S, D[2], None)])
    strategy = ScriptedStrategy(buys=_buy(S, close=10.5), sells={D[2]: [(S, ExitReason.TIME_STOP)]})
    result = _run(tmp_path, [bars], strategy, st_history=st)
    (trade,) = result.trades
    assert trade.exit_day == D[4]
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(10.0, 1000, trade.exit_day))


def test_suspended_position_valued_at_last_close_and_order_deferred(tmp_path: Path) -> None:
    # D[3]..D[7] 停牌：总资产不变；D[8] 复牌开盘 11 成交
    bars = _bars(S, {8: 11.0}, skip=[3, 4, 5, 6, 7])
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[2]: [(S, ExitReason.FUNDAMENTAL)]})
    result = _run(tmp_path, [bars], strategy)
    for i in range(3, 8):
        assert _at(result.equity, D[i]) == pytest.approx(_at(result.equity, D[2]))
        assert _at(result.cash, D[i]) == pytest.approx(_at(result.cash, D[2]))
    (trade,) = result.trades
    assert trade.exit_day == D[8]
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(11.0, 1000, trade.exit_day))


def test_new_position_is_not_sold_on_signal_day_t_plus_1(tmp_path: Path) -> None:
    # D[1] 买入，收盘时就有卖出信号；当天不卖，D[2] 开盘卖出
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[1]: [(S, ExitReason.TIME_STOP)]})
    result = _run(tmp_path, [_bars(S)], strategy)
    (trade,) = result.trades
    assert trade.entry_day == D[1]
    assert trade.exit_day == D[2]
    assert trade.holding_days == 1
    assert _at(result.cash, D[1]) < CASH - 10000  # D[1] 当天仍持有，现金已付出


def test_days_held_and_holding_days(tmp_path: Path) -> None:
    # D[1] 买入，收盘计值 D[1]..D[3] 三次；D[3] 发信号，D[4] 开盘卖出 -> 持有 3 天
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[3]: [(S, ExitReason.TIME_STOP)]})
    (trade,) = _run(tmp_path, [_bars(S)], strategy).trades
    assert trade.holding_days == 3
    # 一直持有到 D[11]：D[1]..D[11] 共 11 次计值；收盘 11 时峰值收益按当日收盘计
    bars = _bars(S, {5: 11.0, 6: 10.0})
    (pos,) = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), name="b").open_positions
    assert pos.days_held == 11
    assert pos.peak_return == pytest.approx(pos.shares * 11.0 / pos.cost - 1)


# ---------- 除权、退市 ----------


class _RealSells:
    """买入用脚本，卖出用真实的高股息低位策略规则。"""

    name = "mixed"

    def __init__(self, buys: Mapping[date, Sequence[BuyCandidate]]) -> None:
        self._scripted = ScriptedStrategy(buys=buys)
        self._real = HighDividendLow(default_strategy_config(), default_universe_config())

    def buy_candidates(
        self, day: date, snapshot: pd.DataFrame, held: AbstractSet[str]
    ) -> list[BuyCandidate]:
        return self._scripted.buy_candidates(day, snapshot, held)

    def sell_signals(
        self, day: date, snapshot: pd.DataFrame, positions: Sequence[Position]
    ) -> list[SellSignal]:
        return self._real.sell_signals(day, snapshot, positions)


def test_ex_dividend_gap_does_not_trigger_stop_loss(tmp_path: Path) -> None:
    # D[2] 除权：未复权价 10 -> 7.5，复权因子 1 -> 4/3；股数放大后收益约为 0
    bars = _bars(S, {i: 7.5 for i in range(2, N)}, adj={i: 4 / 3 for i in range(2, N)})
    result = _run(tmp_path, [bars], _RealSells(_buy(S)))
    assert result.trades == []
    (pos,) = result.open_positions
    assert pos.shares == pytest.approx(1000 * 4 / 3)
    assert abs(pos.ret(7.5)) < 0.01
    assert pos.cost == pytest.approx(COSTS.buy_cost(10.0, 1000))


def test_position_bought_on_ex_dividend_day_is_not_scaled(tmp_path: Path) -> None:
    # D[1] 既是除权日又是买入成交日：新买的股数不放大
    bars = _bars(S, {1: 7.5}, adj={i: 4 / 3 for i in range(1, N)})
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)))
    (pos,) = result.open_positions
    assert pos.entry_day == D[1]
    assert pos.shares == 1000.0


def test_delisted_position_liquidated_at_last_close(tmp_path: Path) -> None:
    # 最后一个行情日 D[4] 收盘 10.5；退市日 D[5]
    bars = _bars(S, {4: 10.5}, skip=range(5, N))
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), delist={S: D[5]})
    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.DELISTED
    assert trade.exit_day == D[5]
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(10.5, 1000, trade.exit_day))
    assert result.open_positions == []
    assert _at(result.equity, D[5]) == pytest.approx(_at(result.cash, D[5]))


def test_delisting_uses_previous_trading_day_close(tmp_path: Path) -> None:
    # 退市日 D[5] 当天仍有一根 9.0 的行情：清算价取前一交易日 D[4] 的收盘 10.5，
    # 因为退市日的收盘价开盘时还不知道
    bars = _bars(S, {4: 10.5, 5: 9.0}, skip=range(6, N))
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), delist={S: D[5]})
    (trade,) = result.trades
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(10.5, 1000, trade.exit_day))


def test_delisting_drops_pending_buy_of_unheld_symbol(tmp_path: Path) -> None:
    # S 在 D[1] 开盘涨停，买单挂着；D[2] 退市（没有持仓）：买单直接作废，既不成交也不过期记录
    bars = _bars(S, {1: 11.0}, skip=range(2, N))
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), delist={S: D[2]})
    assert result.open_positions == []
    assert result.trades == []
    assert result.expired_buys == []


def test_delist_date_on_non_trading_day_liquidates_next_trading_day(tmp_path: Path) -> None:
    friday, saturday, monday = D[3], date(2024, 1, 6), D[4]
    assert friday == date(2024, 1, 5)
    bars = _bars(S, skip=range(4, N))
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), delist={S: saturday})
    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.DELISTED
    assert trade.exit_day == monday
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(10.0, 1000, trade.exit_day))
    assert result.open_positions == []


def test_delisting_drops_pending_sell_order(tmp_path: Path) -> None:
    # 卖出信号后停牌直至退市：只留下退市清算的一笔交易，不重复卖出
    bars = _bars(S, skip=range(4, N))
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[3]: [(S, ExitReason.STOP_LOSS)]})
    result = _run(tmp_path, [bars], strategy, delist={S: D[6]})
    assert [t.exit_reason for t in result.trades] == [ExitReason.DELISTED]


# ---------- 风控联动 ----------


def test_stop_loss_exit_triggers_cooldown(tmp_path: Path) -> None:
    buys = {**_buy(S), **_buy(S, day=4)}
    strategy = ScriptedStrategy(buys=buys, sells={D[2]: [(S, ExitReason.STOP_LOSS)]})
    result = _run(tmp_path, [_bars(S)], strategy)
    assert [t.exit_day for t in result.trades] == [D[3]]
    assert [r.rule for r in result.rejections] == ["cooldown"]
    assert result.open_positions == []


def test_paused_risk_blocks_new_buys_but_allows_sells(tmp_path: Path) -> None:
    # D[2] 收盘 9.0，回撤约 0.9%，超过（测试用的）0.5% 暂停线；暂停当天 D[2] 就有候选，
    # 如果引擎先做第 7 步再做第 5 步，这个候选会变成买单并在 D[3] 成交
    risk_update = {"warn_drawdown": 0.002, "pause_drawdown": 0.005}
    bars = [_bars(S, {2: 9.0, 3: 9.0, 4: 9.0}), _bars(S2)]
    buys = {**_buy(S), **_buy(S2, day=2), **_buy(S2, day=3)}
    strategy = ScriptedStrategy(buys=buys, sells={D[3]: [(S, ExitReason.TIME_STOP)]})
    result = _run(tmp_path, bars, strategy, risk_update=risk_update)
    pauses = [e for e in result.risk_events if e.kind is RiskEventKind.PAUSE]
    assert [e.day for e in pauses] == [D[2]]
    seen_days = [day for day, _ in strategy.held_seen]
    assert D[2] not in seen_days
    assert D[3] not in seen_days  # 暂停期间不再要买入候选
    assert result.rejections == []  # 暂停时不调用 size_buys，不产生 paused 拒绝记录
    assert [t.symbol for t in result.trades] == [S]  # 卖出照常
    assert result.trades[0].exit_day == D[4]
    assert result.open_positions == []  # S2 没有买入


def test_buying_resumes_on_the_evening_of_the_resume_day(tmp_path: Path) -> None:
    # 暂停 2 个交易日：D[2] 暂停，D[4] 收盘恢复。D[3] 的候选被忽略，D[4] 的候选下单，D[5] 成交
    risk_update = {"warn_drawdown": 0.002, "pause_drawdown": 0.005, "pause_days": 2}
    bars = [_bars(S, {i: 9.0 for i in range(2, N)}), _bars(S2)]
    buys = {**_buy(S), **_buy(S2, day=3)}
    buys.update(_buy(S2, day=4))
    strategy = ScriptedStrategy(buys=buys)
    result = _run(tmp_path, bars, strategy, risk_update=risk_update)
    kinds = [(e.day, e.kind) for e in result.risk_events if e.kind is not RiskEventKind.WARN]
    assert kinds == [(D[2], RiskEventKind.PAUSE), (D[4], RiskEventKind.RESUME)]
    seen_days = [day for day, _ in strategy.held_seen]
    assert D[3] not in seen_days
    assert D[4] in seen_days
    entries = {p.symbol: p.entry_day for p in result.open_positions}
    assert entries == {S: D[1], S2: D[5]}


def test_pause_expires_pending_buys(tmp_path: Path) -> None:
    # S 在 D[1] 开盘涨停买不到，买单挂着；同日 S2 成交后收盘跌到 9.0，触发暂停。
    # 暂停开始时所有待成交买单作废：D[2] S 即使可以成交也不买
    risk_update = {"warn_drawdown": 0.002, "pause_drawdown": 0.005}
    bars = [_bars(S, {1: 11.0, 2: 10.5}), _bars(S2, {1: 10.0}, {1: 9.0, 2: 9.0})]
    strategy = ScriptedStrategy(buys=_buy(S, S2))
    result = _run(tmp_path, bars, strategy, risk_update=risk_update)
    assert [p.symbol for p in result.open_positions] == [S2]
    assert [o.symbol for o in result.expired_buys] == [S]


# ---------- 账务、确定性 ----------


def test_round_trip_cash_and_equity_to_the_cent(tmp_path: Path) -> None:
    # 手算：买 1000 股 @10：10010 + 佣金 5 + 过户费 0.1001 = 10015.1001
    # D[1] 收盘 10.5：总资产 = 110000 - 10015.1001 + 10500 = 110484.8999
    # D[3] 发出卖出信号，D[4] 开盘 11 卖出：10989 - (5 + 5.4945 + 0.10989) = 10978.39561
    bars = _bars(S, {1: 10.0, 2: 10.2, 3: 10.8, 4: 11.0}, {1: 10.5, 2: 10.2, 3: 10.8, 4: 11.0})
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[3]: [(S, ExitReason.TAKE_PROFIT_FIXED)]})
    result = _run(tmp_path, [bars], strategy)
    assert _at(result.equity, D[1]) == pytest.approx(110484.8999, abs=1e-6)
    (trade,) = result.trades
    assert trade.cost == pytest.approx(10015.1001, abs=1e-6)
    assert trade.proceeds == pytest.approx(10978.39561, abs=1e-6)
    final_cash = CASH - 10015.1001 + 10978.39561
    assert _at(result.cash, D[4]) == pytest.approx(final_cash, abs=1e-6)
    assert _at(result.equity, D[4]) == pytest.approx(final_cash, abs=1e-6)

    # 每一天：总资产 = 现金 + 持仓市值（D[1]..D[3] 持有 1000 股）
    closes = {1: 10.5, 2: 10.2, 3: 10.8}
    assert list(result.equity.index) == D[:N]
    assert list(result.cash.index) == D[:N]
    assert result.equity.dtype == "float64"
    assert result.cash.dtype == "float64"
    for i, day in enumerate(D[:N]):
        market_value = 1000 * closes[i] if i in closes else 0.0
        assert _at(result.equity, day) == pytest.approx(_at(result.cash, day) + market_value)


def test_held_includes_pending_buys(tmp_path: Path) -> None:
    # D[1] 开盘涨停买不到，买单挂着；D[1] 收盘时 held 应包含该待成交买单的股票
    bars = _bars(S, {1: 11.0})
    strategy = ScriptedStrategy(buys=_buy(S))
    _run(tmp_path, [bars], strategy)
    held = dict(strategy.held_seen)
    assert held[D[0]] == frozenset()
    assert held[D[1]] == frozenset({S})


def test_same_inputs_give_identical_results(tmp_path: Path) -> None:
    def run(name: str) -> BacktestResult:
        bars = [_bars(S, {3: 9.0, 4: 8.1, 5: 8.0}), _bars(S2, {2: 10.4, 6: 10.8})]
        strategy = ScriptedStrategy(
            buys={**_buy(S, S2), **_buy(S, day=7)},
            sells={D[2]: [(S, ExitReason.STOP_LOSS)], D[8]: [(S2, ExitReason.TAKE_PROFIT_FIXED)]},
        )
        return _run(tmp_path, bars, strategy, name=name)

    first, second = run("one"), run("two")
    assert first.trades
    assert first.equity.equals(second.equity)
    assert first.cash.equals(second.cash)
    assert first.trades == second.trades
    assert first.open_positions == second.open_positions
    assert first.rejections == second.rejections
    assert first.risk_events == second.risk_events
    assert first.expired_buys == second.expired_buys


# ---------- 涨跌停价的精确舍入 ----------


def test_limit_prices_match_exact_integer_cent_arithmetic() -> None:
    # 前收 1.00..100.00 逐分扫描；独立用整数分计算：涨跌停 = 前收 * (100±pct)% 逢五进一
    mismatches: list[tuple[int, bool, int, int, int, int]] = []
    for cents in range(100, 10001):
        for is_st, pct in ((False, 10), (True, 5)):
            want_up = (cents * (100 + pct) + 50) // 100
            want_down = (cents * (100 - pct) + 50) // 100
            up, down = limit_prices(cents / 100, is_st)
            got_up, got_down = round(up * 100), round(down * 100)
            if (got_up, got_down) != (want_up, want_down):
                mismatches.append((cents, is_st, got_up, want_up, got_down, want_down))
    assert mismatches == []


def test_limit_down_price_is_rounded_half_up_not_one_cent_low(tmp_path: Path) -> None:
    # 前收 7.85：跌停价 7.065 逢五进一为 7.07（浮点乘积 7.0649999999999995 会误得 7.06）
    # D[3] 开盘 7.07 就是跌停价，卖不出；D[4] 开盘 7.08 才成交
    bars = _bars(S, {0: 7.85, 1: 7.85, 2: 7.85, 3: 7.07, 4: 7.08})
    strategy = ScriptedStrategy(buys=_buy(S, close=7.85), sells={D[2]: [(S, ExitReason.STOP_LOSS)]})
    (trade,) = _run(tmp_path, [bars], strategy).trades
    assert trade.exit_day == D[4]
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(7.08, 1300, trade.exit_day))


def test_st_limit_down_price_inexact_product_is_deferred(tmp_path: Path) -> None:
    # ST 前收 1.30：跌停价 1.235 逢五进一为 1.24；开盘 1.24 是跌停价，顺延
    bars = _bars(S, {0: 1.30, 1: 1.30, 2: 1.30, 3: 1.24, 4: 1.25})
    st = make_st_history([(S, D[0], None)])
    strategy = ScriptedStrategy(buys=_buy(S, close=1.30), sells={D[2]: [(S, ExitReason.STOP_LOSS)]})
    (trade,) = _run(tmp_path, [bars], strategy, st_history=st).trades
    assert trade.exit_day == D[4]


def test_st_limit_up_price_inexact_product_blocks_buy(tmp_path: Path) -> None:
    # ST 前收 1.90：涨停价 1.995 逢五进一为 2.00（浮点乘积 1.9949999999999999 会误得 1.99）
    # D[1] 开盘 2.00 就是涨停价，买不到；D[2] 开盘 1.95 成交
    bars = _bars(S, {0: 1.90, 1: 2.0, 2: 1.95})
    st = make_st_history([(S, D[0], None)])
    strategy = ScriptedStrategy(buys=_buy(S, close=1.90))
    result = _run(tmp_path, [bars], strategy, st_history=st)
    (pos,) = result.open_positions
    assert pos.entry_day == D[2]


def test_st_limit_up_price_below_inexact_product_still_fills(tmp_path: Path) -> None:
    # 同上，涨停价应为 2.00；开盘 1.99 低于涨停价，当日即可成交
    # （浮点乘积会把涨停价算成 1.99，误判为涨停而顺延）
    bars = _bars(S, {0: 1.90, 1: 1.99})
    st = make_st_history([(S, D[0], None)])
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S, close=1.90)), st_history=st)
    (pos,) = result.open_positions
    assert pos.entry_day == D[1]


# ---------- 除权与停牌的组合 ----------


def test_pending_sell_before_ex_dividend_sells_scaled_shares(tmp_path: Path) -> None:
    # D[1] 买入后发出卖出信号（下单时 1000 股）；D[2] 除权，开盘卖出的是放大后的 1333.33 股
    bars = _bars(S, {i: 7.5 for i in range(2, N)}, adj={i: 4 / 3 for i in range(2, N)})
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[1]: [(S, ExitReason.TAKE_PROFIT_FIXED)]})
    (trade,) = _run(tmp_path, [bars], strategy).trades
    assert trade.exit_day == D[2]
    assert trade.proceeds == pytest.approx(COSTS.sell_proceeds(7.5, 1000 * 4 / 3, trade.exit_day))


def test_ex_dividend_during_suspension_applied_once_on_resume_day(tmp_path: Path) -> None:
    # D[2]..D[4] 停牌，复牌日 D[5] 一次性体现因子变化 4/3；此后因子不变，股数只放大一次
    bars = _bars(
        S,
        {i: 7.5 for i in range(5, N)},
        skip=[2, 3, 4],
        adj={i: 4 / 3 for i in range(5, N)},
    )
    result = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S)))
    (pos,) = result.open_positions
    assert pos.shares == pytest.approx(1000 * 4 / 3)


def test_days_held_increments_during_suspension(tmp_path: Path) -> None:
    # 停牌期间持有天数照常增加：D[1]..D[11] 共 11 个交易日
    bars = _bars(S, skip=range(3, 8))
    (pos,) = _run(tmp_path, [bars], ScriptedStrategy(buys=_buy(S))).open_positions
    assert pos.days_held == 11


def _switching_tax(switch_day: date) -> CostsConfig:
    """印花税在 switch_day 由 0.1% 降为 0.05% 的成本配置。"""
    schedule = [
        StampTaxPeriod(start=date(2008, 9, 19), rate=0.001),
        StampTaxPeriod(start=switch_day, rate=0.0005),
    ]
    return default_costs().model_copy(update={"stamp_tax_schedule": schedule})


def test_sell_charged_stamp_tax_in_force_on_fill_day(tmp_path: Path) -> None:
    # D[2] 收盘发出卖出信号，D[3]、D[4] 跌停顺延，D[5] 成交；税率在 D[5] 下调
    bars = _bars(S, {3: 9.0, 4: 8.1, 5: 8.0})
    strategy = ScriptedStrategy(buys=_buy(S), sells={D[2]: [(S, ExitReason.STOP_LOSS)]})
    costs_cfg = _switching_tax(D[5])
    result = _run(tmp_path, [bars], strategy, costs_cfg=costs_cfg)
    (trade,) = result.trades
    model = CostModel(costs_cfg)
    assert trade.exit_day == D[5]
    assert trade.proceeds == pytest.approx(model.sell_proceeds(8.0, 1000, D[5]))
    assert trade.proceeds != pytest.approx(model.sell_proceeds(8.0, 1000, D[2]))


def test_delisting_charged_stamp_tax_in_force_on_liquidation_day(tmp_path: Path) -> None:
    bars = _bars(S, {4: 10.5}, n=5)
    costs_cfg = _switching_tax(D[5])
    result = _run(
        tmp_path, [bars], ScriptedStrategy(buys=_buy(S)), delist={S: D[5]}, costs_cfg=costs_cfg
    )
    (trade,) = result.trades
    model = CostModel(costs_cfg)
    assert trade.exit_day == D[5]
    assert trade.proceeds == pytest.approx(model.sell_proceeds(10.5, 1000, D[5]))
    assert trade.proceeds != pytest.approx(model.sell_proceeds(10.5, 1000, D[4]))
