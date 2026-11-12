"""绩效指标：全部为不依赖回测引擎的纯数值计算。

口径：每年 244 个交易日；夏普取无风险利率 0；最大回撤为负数；
没有交易或没有亏损交易时对应字段为 0，不产生除零告警。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from qt.models import Trade
from qt.risk import Rejection

TRADING_DAYS_PER_YEAR = 244


@dataclass(frozen=True)
class Metrics:
    """一组绩效指标。基准没有交易，交易类字段为 0。"""

    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    profit_loss_ratio: float
    avg_holding_days: float
    annual_turnover: float
    trade_count: int


def _values(series: "pd.Series[float]") -> list[float]:
    return [float(v) for v in series.to_numpy(dtype="float64")]


def _annual_return(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2 or values[0] <= 0 or values[-1] <= 0:
        return 0.0
    return (values[-1] / values[0]) ** (TRADING_DAYS_PER_YEAR / (n - 1)) - 1


def _max_drawdown(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    arr = np.asarray(values, dtype="float64")
    peak = np.maximum.accumulate(arr)
    return float(np.min(arr / peak - 1))


def _sharpe(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    arr = np.asarray(values, dtype="float64")
    rets = arr[1:] / arr[:-1] - 1
    std = float(np.std(rets, ddof=1))
    if std == 0.0 or not math.isfinite(std):
        return 0.0
    return float(np.mean(rets)) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def _price_metrics(values: Sequence[float]) -> tuple[float, float, float]:
    return _annual_return(values), _max_drawdown(values), _sharpe(values)


def compute_metrics(equity: "pd.Series[float]", trades: Sequence[Trade]) -> Metrics:
    """由每日总资产与已平仓交易计算绩效指标。"""
    values = _values(equity)
    annual, drawdown, sharpe = _price_metrics(values)
    count = len(trades)
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / count if count else 0.0
    ratio = 0.0
    if wins and losses:
        ratio = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    avg_days = sum(t.holding_days for t in trades) / count if count else 0.0
    turnover = 0.0
    if len(values) >= 2 and count:
        mean_equity = sum(values) / len(values)
        if mean_equity > 0:
            sold = sum(t.proceeds for t in trades)
            turnover = sold / mean_equity / (len(values) - 1) * TRADING_DAYS_PER_YEAR
    return Metrics(annual, drawdown, sharpe, win_rate, ratio, avg_days, turnover, count)


def benchmark_metrics(close: "pd.Series[float]") -> Metrics:
    """基准只有收益、回撤、夏普，交易类字段填 0。"""
    annual, drawdown, sharpe = _price_metrics(_values(close))
    return Metrics(annual, drawdown, sharpe, 0.0, 0.0, 0.0, 0.0, 0)


def exit_breakdown(trades: Sequence[Trade]) -> pd.DataFrame:
    """按退出原因汇总。列：exit_reason, count, avg_ret, total_pnl。"""
    rows: dict[str, list[Trade]] = {}
    for t in trades:
        rows.setdefault(t.exit_reason.value, []).append(t)
    return pd.DataFrame(
        {
            "exit_reason": pd.Series(list(rows), dtype="str"),
            "count": pd.Series([len(v) for v in rows.values()], dtype="int64"),
            "avg_ret": pd.Series(
                [sum(t.ret for t in v) / len(v) for v in rows.values()], dtype="float64"
            ),
            "total_pnl": pd.Series([sum(t.pnl for t in v) for v in rows.values()], dtype="float64"),
        }
    )


def rejection_breakdown(rejections: Sequence[Rejection]) -> pd.DataFrame:
    """按拒绝规则计数。列：rule, count。"""
    counts: dict[str, int] = {}
    for r in rejections:
        counts[r.rule] = counts.get(r.rule, 0) + 1
    return pd.DataFrame(
        {
            "rule": pd.Series(list(counts), dtype="str"),
            "count": pd.Series(list(counts.values()), dtype="int64"),
        }
    )
