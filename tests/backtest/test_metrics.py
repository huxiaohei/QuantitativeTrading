"""绩效指标测试。"""

import math
from datetime import date

import pandas as pd
import pytest

from qt.backtest.metrics import (
    benchmark_metrics,
    compute_metrics,
    exit_breakdown,
    rejection_breakdown,
)
from qt.models import ExitReason, Trade
from qt.risk import Rejection
from tests.fakes import make_candidate, weekdays


def _series(values: list[float]) -> "pd.Series[float]":
    days = weekdays(date(2024, 1, 2), len(values))
    return pd.Series(values, index=pd.Index(days), dtype="float64")


def _trade(pnl: float, reason: ExitReason = ExitReason.TAKE_PROFIT_FIXED, days: int = 10) -> Trade:
    """成本固定 1000，盈亏为 pnl。"""
    return Trade(
        symbol="600000",
        entry_day=date(2024, 1, 2),
        exit_day=date(2024, 2, 1),
        cost=1000.0,
        proceeds=1000.0 + pnl,
        exit_reason=reason,
        holding_days=days,
        entry_metrics={},
    )


def test_max_drawdown() -> None:
    m = compute_metrics(_series([100, 110, 99, 121]), [])
    assert m.max_drawdown == pytest.approx(-0.10)


def test_annual_return_over_244_days() -> None:
    values = [100.0] * 244 + [110.0]
    m = compute_metrics(_series(values), [])
    assert m.annual_return == pytest.approx(0.10)


def test_sharpe_on_known_series() -> None:
    equity = _series([100.0, 101.0, 100.0, 102.0])
    rets = [101 / 100 - 1, 100 / 101 - 1, 102 / 100 - 1]
    mean = sum(rets) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / 2)
    m = compute_metrics(equity, [])
    assert m.sharpe == pytest.approx(mean / std * math.sqrt(244))


def test_sharpe_zero_when_flat_or_short() -> None:
    assert compute_metrics(_series([100.0, 100.0, 100.0, 100.0]), []).sharpe == 0.0
    assert compute_metrics(_series([100.0, 101.0]), []).sharpe == 0.0


def test_win_rate_and_profit_loss_ratio() -> None:
    m = compute_metrics(_series([100, 101, 102]), [_trade(200), _trade(100), _trade(-100)])
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.profit_loss_ratio == pytest.approx(1.5)
    assert m.trade_count == 3


def test_no_trades_gives_zeros_without_warning() -> None:
    m = compute_metrics(_series([100.0, 100.0, 100.0]), [])
    assert (m.win_rate, m.profit_loss_ratio, m.avg_holding_days) == (0.0, 0.0, 0.0)
    assert (m.annual_turnover, m.trade_count) == (0.0, 0)
    only_wins = compute_metrics(_series([100.0, 101.0, 102.0]), [_trade(50)])
    assert only_wins.profit_loss_ratio == 0.0
    single = compute_metrics(_series([100.0]), [])
    assert (single.annual_return, single.max_drawdown, single.sharpe) == (0.0, 0.0, 0.0)


def test_avg_holding_days_and_annual_turnover() -> None:
    # 3 个点，n-1=2；平均资产 100；卖出总额 (1100 + 900) = 2000
    trades = [_trade(100, days=10), _trade(-100, days=30)]
    m = compute_metrics(_series([100.0, 100.0, 100.0]), trades)
    assert m.avg_holding_days == pytest.approx(20.0)
    assert m.annual_turnover == pytest.approx(2000 / 100 / 2 * 244)


def test_benchmark_metrics_zero_trade_fields() -> None:
    m = benchmark_metrics(_series([100, 110, 99, 121]))
    assert m.max_drawdown == pytest.approx(-0.10)
    assert m.annual_return == pytest.approx((1.21) ** (244 / 3) - 1)
    assert (m.win_rate, m.profit_loss_ratio, m.avg_holding_days) == (0.0, 0.0, 0.0)
    assert (m.annual_turnover, m.trade_count) == (0.0, 0)


def test_exit_breakdown_groups_by_reason() -> None:
    trades = [
        _trade(100, ExitReason.TAKE_PROFIT_FIXED),
        _trade(200, ExitReason.TAKE_PROFIT_FIXED),
        _trade(-150, ExitReason.STOP_LOSS),
    ]
    df = exit_breakdown(trades).set_index("exit_reason")
    assert list(exit_breakdown(trades).columns) == ["exit_reason", "count", "avg_ret", "total_pnl"]
    assert df.loc["take_profit_fixed", "count"] == 2
    assert df.loc["take_profit_fixed", "avg_ret"] == pytest.approx(0.15)
    assert df.loc["take_profit_fixed", "total_pnl"] == pytest.approx(300.0)
    assert df.loc["stop_loss", "total_pnl"] == pytest.approx(-150.0)
    assert len(exit_breakdown([])) == 0


def test_rejection_breakdown_counts_by_rule() -> None:
    cand = make_candidate("600000")
    rejections = [
        Rejection(cand, "cooldown", "x"),
        Rejection(cand, "cooldown", "x"),
        Rejection(cand, "no_cash", "x"),
    ]
    df = rejection_breakdown(rejections)
    assert list(df.columns) == ["rule", "count"]
    assert dict(zip(df["rule"], df["count"], strict=True)) == {"cooldown": 2, "no_cash": 1}
    assert len(rejection_breakdown([])) == 0
