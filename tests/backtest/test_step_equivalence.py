"""逐日推进等价性：每天把引擎与风控状态过一遍 JSON、新建策略与风控对象，
结果必须与一次性 run_backtest 完全相同（浮点逐位相等，成交、事件逐项相同）。"""

import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qt.backtest.engine import BacktestResult, DayLog, EngineState, run_backtest, step_day
from qt.backtest.report import clamp_period
from qt.backtest.state import dump_state, load_state
from qt.backtest.sweep import override
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.costs import CostModel
from qt.data.store import ParquetStore
from qt.features import FeaturePanel, build_panel
from qt.models import BuyCandidate, ExitReason, Order, Position, Side
from qt.risk import RiskEventKind, RiskManager, RiskState
from qt.strategy.high_dividend_low import HighDividendLow
from tests.fakes import default_app_config, populate_trading_store

OVERRIDES: list[dict[str, object]] = [
    {},
    {"risk.pause_drawdown": 0.05, "risk.warn_drawdown": 0.03},
    {"strategy.take_profit_mode": "trailing"},
    {"risk.cooldown_days": 0},
    # 数据集最大回撤约 3%，上面 5% 的暂停线碰不到；这一组会两次暂停、两次恢复
    {"risk.pause_drawdown": 0.02, "risk.warn_drawdown": 0.01},
]


@dataclass(frozen=True)
class World:
    """共用的数据集：日历、指标面板与回测区间。"""

    calendar: TradingCalendar
    panel: FeaturePanel
    days: list[date]


@dataclass(frozen=True)
class Run:
    """同一组参数下一次性回测与逐日推进的结果。"""

    whole: BacktestResult
    logs: list[DayLog]
    texts: list[str]  # 每天推进后序列化出的 JSON 文本
    final: EngineState


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    root: Path = tmp_path_factory.mktemp("data")
    store = ParquetStore(root)
    days = populate_trading_store(store)
    # 样本内起点一直延伸到数据集末尾，覆盖暴跌后的止损与暂停
    calendar, start, end = clamp_period(store, days[0], days[-1])
    panel = build_panel(store, calendar, default_app_config(), start, end)
    return World(calendar, panel, calendar.between(start, end))


def _config(overrides: dict[str, object]) -> AppConfig:
    cfg = default_app_config()
    for path, value in overrides.items():
        cfg = override(cfg, path, value)
    return cfg


def fresh_strategy(cfg: AppConfig) -> HighDividendLow:
    return HighDividendLow(cfg.strategy, cfg.universe)


def fresh_risk(cfg: AppConfig, calendar: TradingCalendar) -> RiskManager:
    return RiskManager(cfg.risk, calendar, CostModel(cfg.costs))


def pairwise_days(days: Sequence[date]) -> Iterator[tuple[date | None, date]]:
    """(上一交易日, 当天)；首日的上一交易日为 None。"""
    prev: date | None = None
    for day in days:
        yield prev, day
        prev = day


def result_from_logs(logs: Sequence[DayLog], final: EngineState) -> BacktestResult:
    """把逐日日志汇总成 BacktestResult（独立于引擎内部的汇总实现）。"""
    index = pd.Index([log.day for log in logs])
    return BacktestResult(
        equity=pd.Series([log.equity for log in logs], index=index, dtype="float64"),
        cash=pd.Series([log.cash for log in logs], index=index, dtype="float64"),
        trades=[t for log in logs for t in log.trades],
        open_positions=list(final.positions.values()),
        rejections=[r for log in logs for r in log.rejections],
        risk_events=[e for log in logs for e in log.risk_events],
        expired_buys=[o for log in logs for o in log.expired_buys],
    )


def _empty_state(cash: float) -> EngineState:
    return EngineState(cash=cash, positions={}, sell_orders=[], buy_orders=[], candidates={})


def _run(world: World, overrides: dict[str, object]) -> Run:
    cfg = _config(overrides)
    costs = CostModel(cfg.costs)
    cash = cfg.backtest.initial_cash
    valid = cfg.backtest.buy_order_valid_days
    cal = world.calendar
    whole = run_backtest(
        world.panel,
        fresh_strategy(cfg),
        fresh_risk(cfg, cal),
        costs,
        cal,
        world.days[0],
        world.days[-1],
        cash,
        valid,
    )
    text = json.dumps(dump_state(_empty_state(cash), fresh_risk(cfg, cal).dump_state()))
    logs: list[DayLog] = []
    texts: list[str] = []
    for prev, day in pairwise_days(world.days):
        state, rstate = load_state(json.loads(text))  # 每天都过一遍 JSON
        risk = fresh_risk(cfg, cal)  # 每天新建风控与策略对象
        risk.load_state(rstate)
        logs.append(
            step_day(
                state, world.panel, fresh_strategy(cfg), risk, costs, cal, day, prev, cash, valid
            )
        )
        # allow_nan=False：保证是严格的 JSON（数据库 JSONB 不接受 NaN）
        text = json.dumps(dump_state(state, risk.dump_state()), allow_nan=False)
        texts.append(text)
    final, _ = load_state(json.loads(text))
    return Run(whole, logs, texts, final)


@pytest.fixture(scope="module")
def runs(world: World) -> list[Run]:
    return [_run(world, o) for o in OVERRIDES]


def _assert_same(a: BacktestResult, b: BacktestResult) -> None:
    pd.testing.assert_series_equal(a.equity, b.equity, check_exact=True)
    pd.testing.assert_series_equal(a.cash, b.cash, check_exact=True)
    assert a.trades == b.trades
    assert a.rejections == b.rejections
    assert a.risk_events == b.risk_events
    assert a.expired_buys == b.expired_buys
    assert a.open_positions == b.open_positions


@pytest.mark.parametrize("index", range(len(OVERRIDES)), ids=[str(o) for o in OVERRIDES])
def test_stepping_with_json_roundtrip_every_day_equals_run_backtest(
    runs: list[Run], index: int
) -> None:
    run = runs[index]
    _assert_same(run.whole, result_from_logs(run.logs, run.final))
    # 逐位相等：浮点的二进制表示也一致
    assert [v.hex() for v in run.whole.equity] == [log.equity.hex() for log in run.logs]


def test_fixture_exercises_stop_loss_take_profit_pause_and_pending_orders(
    runs: list[Run],
) -> None:
    # 防止等价测试退化：至少一组参数下出现止盈、止损、PAUSE 事件，
    # 且至少有一天序列化时 buy_orders 或 sell_orders 非空
    reasons = {t.exit_reason for run in runs for t in run.whole.trades}
    assert ExitReason.STOP_LOSS in reasons
    assert reasons & {ExitReason.TAKE_PROFIT_FIXED, ExitReason.TAKE_PROFIT_TRAILING}
    kinds = {e.kind for run in runs for e in run.whole.risk_events}
    assert RiskEventKind.PAUSE in kinds
    pending_buys = pending_sells = paused = cooling = 0
    for run in runs:
        for text in run.texts:
            raw = json.loads(text)
            pending_buys += bool(raw["engine"]["buy_orders"])
            pending_sells += bool(raw["engine"]["sell_orders"])
            paused += raw["risk"]["paused"] is True
            cooling += bool(raw["risk"]["exits"])
    # 暂停与冷却记录也确实经过了序列化
    assert pending_buys > 0 and pending_sells > 0 and paused > 0 and cooling > 0
    fills = [f for run in runs for log in run.logs for f in log.fills]
    assert {f.side for f in fills} == {Side.BUY, Side.SELL}


def test_fills_match_trades_and_positions(runs: list[Run]) -> None:
    for run in runs:
        fills = [f for log in run.logs for f in log.fills]
        sells = [f for f in fills if f.side is Side.SELL]
        buys = [f for f in fills if f.side is Side.BUY]
        trades = run.whole.trades
        assert [(f.symbol, f.day, f.amount) for f in sells] == [
            (t.symbol, t.exit_day, t.proceeds) for t in trades
        ]
        bought = [*trades, *run.whole.open_positions]
        assert sorted((f.day, f.symbol, f.amount) for f in buys) == sorted(
            (x.entry_day, x.symbol, x.cost) for x in bought
        )
        assert all(f.signal_day < f.day and f.price > 0 for f in fills)


# ---- 序列化细节 ----


def _position(**metrics: float) -> Position:
    return Position(
        symbol="600001",
        shares=1400.0000000000002,
        cost=14_021.371,
        entry_day=date(2022, 1, 5),
        industry="银行",
        entry_metrics=metrics or {"dividend_yield": 0.1 + 0.2, "roe_avg3": 1 / 3},
        days_held=7,
        peak_return=-0.0123456789,
    )


def _rich_state() -> tuple[EngineState, RiskState]:
    candidate = BuyCandidate(
        symbol="600002",
        day=date(2022, 1, 6),
        close=10.07,
        industry="煤炭",
        metrics={"z": 1.0, "a": 2.0},  # 键序不是字母序，验证按原顺序保存
        reason="股息率 6.0%",
    )
    state = EngineState(
        cash=0.1 + 0.2,
        positions={"600001": _position()},
        sell_orders=[
            Order("600001", Side.SELL, 1400.0, date(2022, 1, 6), "止损", ExitReason.STOP_LOSS)
        ],
        buy_orders=[Order("600002", Side.BUY, 1400, date(2022, 1, 6), "股息率 6.0%")],
        candidates={"600002": candidate},
    )
    risk = RiskState(
        peak=1.0000000000000002,
        warned=True,
        paused=True,
        pause_day=date(2022, 1, 4),
        pause_drawdown=-0.2000000000000001,
        pause_days=65,
        exits=(("600003", date(2022, 1, 3)), ("600001", date(2021, 12, 1))),
    )
    return state, risk


def _roundtrip(state: EngineState, risk: RiskState) -> tuple[EngineState, RiskState]:
    return load_state(json.loads(json.dumps(dump_state(state, risk), allow_nan=False)))


def test_roundtrip_preserves_every_value_and_order_exactly() -> None:
    state, risk = _rich_state()
    loaded, loaded_risk = _roundtrip(state, risk)
    assert loaded == state
    assert loaded_risk == risk
    assert loaded.cash.hex() == state.cash.hex()
    assert list(loaded.candidates["600002"].metrics) == ["z", "a"]
    assert type(loaded.buy_orders[0].shares) is int  # 整数股数仍是 int，与原状态完全一致
    assert type(loaded.sell_orders[0].shares) is float


def test_dump_state_covers_every_dataclass_field() -> None:
    state, risk = _rich_state()
    raw = json.loads(json.dumps(dump_state(state, risk)))
    engine = raw["engine"]
    assert set(engine) == {f.name for f in fields(EngineState)}
    assert set(raw["risk"]) == {f.name for f in fields(RiskState)}
    [[key, position]] = engine["positions"]
    assert key == "600001"
    assert set(position) == {f.name for f in fields(Position)}
    assert set(engine["buy_orders"][0]) == {f.name for f in fields(Order)}
    [[_, candidate]] = engine["candidates"]
    assert set(candidate) == {f.name for f in fields(BuyCandidate)}


def test_load_state_falls_back_to_defaults_for_absent_optional_fields() -> None:
    state, risk = _rich_state()
    raw = json.loads(json.dumps(dump_state(state, risk)))
    position = raw["engine"]["positions"][0][1]
    del position["days_held"], position["peak_return"]
    del raw["engine"]["buy_orders"][0]["exit_reason"]
    loaded, _ = load_state(raw)
    pos = loaded.positions["600001"]
    assert (pos.days_held, pos.peak_return) == (0, 0.0)
    assert loaded.buy_orders[0].exit_reason is None


def test_load_state_rejects_missing_required_and_unknown_fields() -> None:
    state, risk = _rich_state()
    raw = json.loads(json.dumps(dump_state(state, risk)))
    del raw["engine"]["positions"][0][1]["cost"]
    with pytest.raises(ValueError, match="cost"):
        load_state(raw)
    raw = json.loads(json.dumps(dump_state(state, risk)))
    raw["engine"]["positions"][0][1]["bogus"] = 1
    with pytest.raises(ValueError, match="bogus"):
        load_state(raw)
    raw = json.loads(json.dumps(dump_state(state, risk)))
    raw["version"] = 999
    with pytest.raises(ValueError, match="999"):
        load_state(raw)


def test_non_finite_metrics_survive_as_strict_json() -> None:
    # 金融股豁免资产负债率，debt_ratio 可能是 NaN 也会进入 metrics
    state, risk = _rich_state()
    state.positions["600001"] = _position(debt_ratio=math.nan, a=math.inf, b=-math.inf)
    loaded, _ = _roundtrip(state, risk)
    metrics = loaded.positions["600001"].entry_metrics
    assert math.isnan(metrics["debt_ratio"])
    assert metrics["a"] == math.inf and metrics["b"] == -math.inf
