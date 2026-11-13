"""高股息低位策略的买入与卖出规则测试。"""

import math
from datetime import date

import pandas as pd
import pytest

from qt.config import StrategyConfig
from qt.models import ExitReason, Position
from qt.strategy.high_dividend_low import HighDividendLow
from tests.fakes import (
    default_strategy_config,
    default_universe_config,
    make_position,
    make_snapshot,
)

DAY = date(2024, 3, 1)


def _strategy(cfg: StrategyConfig | None = None) -> HighDividendLow:
    return HighDividendLow(cfg or default_strategy_config(), default_universe_config())


def _trailing() -> HighDividendLow:
    cfg = default_strategy_config().model_copy(update={"take_profit_mode": "trailing"})
    return _strategy(cfg)


def _pos(
    ret: float = 0.0, days_held: int = 10, peak_return: float = 0.0, symbol: str = "600000"
) -> Position:
    """构造持仓：成本 1000、股数 100，收盘价 10 时收益率为 ret 对应的价格见 _close。"""
    pos = make_position(symbol, shares=100, cost=1000.0)
    pos.days_held = days_held
    pos.peak_return = peak_return
    return pos


def _close(ret: float) -> float:
    return 10.0 * (1 + ret)


# ---------- 买入 ----------


def test_selects_stock_meeting_all_conditions() -> None:
    snap = make_snapshot({"600000": {}})
    result = _strategy().buy_candidates(DAY, snap, set())
    assert len(result) == 1
    c = result[0]
    assert c.symbol == "600000"
    assert c.day == DAY
    assert c.close == 10.0
    assert c.industry == "银行"
    assert c.metrics == {
        "dividend_yield": 0.052,
        "price_position": 0.12,
        "dividend_years": 8.0,
        "roe_avg3": 0.12,
        "payout_ratio": 0.5,
        "profit_yoy": 0.05,
        "debt_ratio": 0.5,
    }


def test_boundary_values_are_inclusive() -> None:
    snap = make_snapshot(
        {
            "600000": {
                "dividend_yield": 0.04,
                "price_position": 0.2,
                "dividend_years": 5,
                "roe_avg3": 0.08,
                "payout_ratio": 0.80,
                "profit_yoy": -0.20,
                "debt_ratio": 0.70,
                "listed_years": 3.0,
                "avg_amount_60d": 5e7,
            }
        }
    )
    assert [c.symbol for c in _strategy().buy_candidates(DAY, snap, set())] == ["600000"]


@pytest.mark.parametrize(
    "column,value",
    [
        ("dividend_yield", 0.039),
        ("price_position", 0.21),
        ("dividend_years", 4),
        ("roe_avg3", 0.079),
        ("payout_ratio", 0.81),
        ("profit_yoy", -0.21),
        ("debt_ratio", 0.71),
        ("is_st", True),
        ("listed_years", 2.9),
        ("avg_amount_60d", 4.9e7),
    ],
)
def test_rejects_when_one_condition_fails(column: str, value: object) -> None:
    snap = make_snapshot({"600000": {column: value}, "600001": {}})
    result = _strategy().buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600001"]


def test_nan_metric_is_rejected_not_error() -> None:
    snap = make_snapshot({"600000": {"roe_avg3": math.nan}, "600001": {}})
    result = _strategy().buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600001"]


@pytest.mark.parametrize(
    "column",
    [
        "dividend_yield",
        "price_position",
        "payout_ratio",
        "profit_yoy",
        "debt_ratio",
        "listed_years",
        "avg_amount_60d",
    ],
)
def test_any_nan_metric_rejects(column: str) -> None:
    snap = make_snapshot({"600000": {column: math.nan}})
    assert _strategy().buy_candidates(DAY, snap, set()) == []


def _exclude_cut(enabled: bool) -> HighDividendLow:
    cfg = default_strategy_config().model_copy(update={"exclude_dividend_cut": enabled})
    return _strategy(cfg)


def test_default_config_excludes_dividend_cut() -> None:
    assert default_strategy_config().exclude_dividend_cut is True


def test_dividend_cut_candidate_rejected_when_switch_on() -> None:
    snap = make_snapshot({"600000": {"dividend_cut": True}, "600001": {}})
    result = _exclude_cut(True).buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600001"]


def test_dividend_cut_candidate_accepted_when_switch_off() -> None:
    snap = make_snapshot({"600000": {"dividend_cut": True}, "600001": {}})
    result = _exclude_cut(False).buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600000", "600001"]


def test_missing_dividend_cut_flag_does_not_pass_as_no_cut() -> None:
    snap = make_snapshot({"600000": {}, "600001": {}})
    snap["dividend_cut"] = pd.Series([math.nan, False], index=snap.index, dtype="object")
    result = _exclude_cut(True).buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600001"]


def test_financial_stock_exempt_from_debt_ratio() -> None:
    snap = make_snapshot(
        {
            "600000": {"is_financial": True, "debt_ratio": 0.92},
            "600001": {"is_financial": False, "debt_ratio": 0.92},
            "600002": {"is_financial": True, "debt_ratio": math.nan},
        }
    )
    result = _strategy().buy_candidates(DAY, snap, set())
    assert [c.symbol for c in result] == ["600000", "600002"]
    assert math.isnan(result[1].metrics["debt_ratio"])


def test_financial_stock_still_needs_other_conditions() -> None:
    snap = make_snapshot({"600000": {"is_financial": True, "debt_ratio": 0.92, "roe_avg3": 0.01}})
    assert _strategy().buy_candidates(DAY, snap, set()) == []


def test_missing_industry_uses_empty_string() -> None:
    snap = make_snapshot({"600000": {"industry": math.nan}, "600001": {"industry": ""}})
    result = _strategy().buy_candidates(DAY, snap, set())
    assert [c.industry for c in result] == ["", ""]


def test_held_symbols_excluded() -> None:
    snap = make_snapshot({"600000": {}, "600001": {}})
    result = _strategy().buy_candidates(DAY, snap, {"600000"})
    assert [c.symbol for c in result] == ["600001"]


def test_sorted_by_dividend_yield_desc() -> None:
    snap = make_snapshot(
        {
            "600003": {"dividend_yield": 0.06},
            "600001": {"dividend_yield": 0.05},
            "600002": {"dividend_yield": 0.09},
            "600000": {"dividend_yield": 0.05},
        }
    )
    result = _strategy().buy_candidates(DAY, snap, set())
    # 股息率相同按代码升序
    assert [c.symbol for c in result] == ["600002", "600003", "600000", "600001"]


def test_candidate_reason_mentions_key_numbers() -> None:
    snap = make_snapshot({"600000": {"dividend_yield": 0.052, "price_position": 0.12}})
    (c,) = _strategy().buy_candidates(DAY, snap, set())
    assert c.reason == "股息率 5.2%，处于一年价格区间底部 12%"


def test_empty_snapshot_yields_no_candidates() -> None:
    assert _strategy().buy_candidates(DAY, make_snapshot({}), set()) == []


def test_thresholds_come_from_config() -> None:
    cfg = default_strategy_config().model_copy(update={"min_dividend_yield": 0.06})
    snap = make_snapshot({"600000": {"dividend_yield": 0.05}})
    assert _strategy(cfg).buy_candidates(DAY, snap, set()) == []


# ---------- 卖出 ----------


def test_no_signal_when_nothing_triggers() -> None:
    snap = make_snapshot({"600000": {"close": _close(0.05)}})
    assert _strategy().sell_signals(DAY, snap, [_pos()]) == []


def test_fixed_take_profit_at_12_percent() -> None:
    pos = _pos()
    hit = make_snapshot({"600000": {"close": 11.2}})
    (sig,) = _strategy().sell_signals(DAY, hit, [pos])
    assert sig.reason is ExitReason.TAKE_PROFIT_FIXED
    assert sig.symbol == "600000"
    assert sig.day == DAY
    assert "12.0%" in sig.detail
    miss = make_snapshot({"600000": {"close": 11.19}})
    assert _strategy().sell_signals(DAY, miss, [pos]) == []


def test_trailing_take_profit() -> None:
    snap = make_snapshot({"600000": {"close": 11.0}})
    (sig,) = _trailing().sell_signals(DAY, snap, [_pos(peak_return=0.20)])
    assert sig.reason is ExitReason.TAKE_PROFIT_TRAILING
    assert "20.0%" in sig.detail

    # 峰值收益 0.09 未达启动线 0.10
    flat = make_snapshot({"600000": {"close": 10.0}})
    assert _trailing().sell_signals(DAY, flat, [_pos(peak_return=0.09)]) == []


def test_trailing_not_triggered_when_drawdown_small() -> None:
    # 峰值 0.20、当前 0.12：(1.12/1.20)-1 = -6.7%，未到 -8%
    snap = make_snapshot({"600000": {"close": 11.2}})
    assert _trailing().sell_signals(DAY, snap, [_pos(peak_return=0.20)]) == []


def test_trailing_mode_ignores_fixed_threshold() -> None:
    # 收益 0.15 超过固定线，但 trailing 模式不看固定线
    snap = make_snapshot({"600000": {"close": 11.5}})
    assert _trailing().sell_signals(DAY, snap, [_pos(peak_return=0.15)]) == []


def test_fixed_mode_ignores_trailing_rule() -> None:
    # 回落 8.3% 满足 trailing，但 fixed 模式下收益 0.10 < 0.12 不触发
    snap = make_snapshot({"600000": {"close": 11.0}})
    assert _strategy().sell_signals(DAY, snap, [_pos(peak_return=0.20)]) == []


def test_stop_loss_at_15_percent() -> None:
    hit = make_snapshot({"600000": {"close": 8.5}})
    (sig,) = _strategy().sell_signals(DAY, hit, [_pos()])
    assert sig.reason is ExitReason.STOP_LOSS
    assert "-15.0%" in sig.detail
    miss = make_snapshot({"600000": {"close": 8.51}})
    assert _strategy().sell_signals(DAY, miss, [_pos()]) == []


def test_time_stop_only_when_not_profitable() -> None:
    s = _strategy()
    loss = make_snapshot({"600000": {"close": _close(-0.01)}})
    (sig,) = s.sell_signals(DAY, loss, [_pos(days_held=250)])
    assert sig.reason is ExitReason.TIME_STOP
    assert "250" in sig.detail
    gain = make_snapshot({"600000": {"close": _close(0.01)}})
    assert s.sell_signals(DAY, gain, [_pos(days_held=250)]) == []
    # 未满 250 天不触发
    assert s.sell_signals(DAY, loss, [_pos(days_held=249)]) == []


def test_time_stop_triggers_at_zero_return() -> None:
    snap = make_snapshot({"600000": {"close": 10.0}})
    (sig,) = _strategy().sell_signals(DAY, snap, [_pos(days_held=250)])
    assert sig.reason is ExitReason.TIME_STOP


def test_fundamental_stop_on_dividend_cut_or_profit_drop() -> None:
    s = _strategy()
    cut = make_snapshot(
        {
            "600000": {
                "dividend_cut": True,
                "dividend_fy_total": 0.3,
                "dividend_prev_fy_total": 0.5,
                "dividend_open_fy_total": 0.0,
            }
        }
    )
    (sig,) = s.sell_signals(DAY, cut, [_pos()])
    assert sig.reason is ExitReason.FUNDAMENTAL
    assert sig.detail == (
        "当前分红水平每股 0.30 元（最近完整年度 0.30 元，本年度已公告 0.00 元），"
        "低于上一年度的 0.50 元（均按当前股本折算）"
    )

    drop = make_snapshot({"600000": {"profit_yoy": -0.31}})
    (sig,) = s.sell_signals(DAY, drop, [_pos()])
    assert sig.reason is ExitReason.FUNDAMENTAL
    assert "-31.0%" in sig.detail
    assert "-30.0%" in sig.detail

    # 恰为 -0.30 不触发（严格小于）
    edge = make_snapshot({"600000": {"profit_yoy": -0.30}})
    assert s.sell_signals(DAY, edge, [_pos()]) == []


def test_nan_profit_yoy_does_not_trigger_fundamental_stop() -> None:
    snap = make_snapshot({"600000": {"profit_yoy": math.nan}})
    assert _strategy().sell_signals(DAY, snap, [_pos()]) == []


def test_fundamental_takes_priority_over_stop_loss() -> None:
    snap = make_snapshot({"600000": {"close": 8.0, "dividend_cut": True}})
    (sig,) = _strategy().sell_signals(DAY, snap, [_pos(days_held=300)])
    assert sig.reason is ExitReason.FUNDAMENTAL


def test_stop_loss_takes_priority_over_time_stop() -> None:
    snap = make_snapshot({"600000": {"close": 8.0}})
    (sig,) = _strategy().sell_signals(DAY, snap, [_pos(days_held=300)])
    assert sig.reason is ExitReason.STOP_LOSS


def test_suspended_position_yields_no_signal() -> None:
    snap = make_snapshot({"600001": {}})
    assert _strategy().sell_signals(DAY, snap, [_pos(symbol="600000")]) == []


def test_sell_signals_do_not_mutate_positions() -> None:
    pos = _pos(days_held=250, peak_return=0.2)
    snap = make_snapshot({"600000": {"close": 8.0}})
    _strategy().sell_signals(DAY, snap, [pos])
    assert (pos.shares, pos.cost, pos.days_held, pos.peak_return) == (100, 1000.0, 250, 0.2)


def test_multiple_positions_each_checked() -> None:
    snap = make_snapshot(
        {"600000": {"close": 11.2}, "600001": {"close": 10.0}, "600002": {"close": 8.0}}
    )
    positions = [_pos(symbol="600000"), _pos(symbol="600001"), _pos(symbol="600002")]
    result = _strategy().sell_signals(DAY, snap, positions)
    assert [(s.symbol, s.reason) for s in result] == [
        ("600000", ExitReason.TAKE_PROFIT_FIXED),
        ("600002", ExitReason.STOP_LOSS),
    ]
