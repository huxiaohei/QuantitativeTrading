"""高股息低位策略：买入高股息、处于一年价格区间低位的稳健公司，按固定或回落规则卖出。

本模块不依赖回测引擎，回测与将来的实盘共用；所有阈值读自配置。
"""

import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import date
from typing import SupportsFloat, cast

import pandas as pd

from qt.config import StrategyConfig, UniverseConfig
from qt.models import BuyCandidate, ExitReason, Position, SellSignal

# 记录进 BuyCandidate.metrics 的指标列
_METRIC_COLUMNS = (
    "dividend_yield",
    "price_position",
    "dividend_years",
    "roe_avg3",
    "payout_ratio",
    "profit_yoy",
    "debt_ratio",
)


def _to_float(value: object) -> float:
    """把单元格值转成 float；无法转换或缺失时返回 NaN。"""
    try:
        return float(cast(SupportsFloat, value))
    except (TypeError, ValueError):
        return math.nan


def _cell(snapshot: pd.DataFrame, symbol: str, column: str) -> object:
    """读取快照中某只股票某列的原始值。"""
    return cast(object, snapshot.at[symbol, column])


def _float_at(snapshot: pd.DataFrame, symbol: str, column: str) -> float:
    return _to_float(_cell(snapshot, symbol, column))


def _industry_at(snapshot: pd.DataFrame, symbol: str) -> str:
    """行业名；缺失（空值）时用空字符串。"""
    value = _cell(snapshot, symbol, "industry")
    return value if isinstance(value, str) else ""


def _float_column(snapshot: pd.DataFrame, column: str) -> "pd.Series[float]":
    """取一列并转为 float64，便于比较时空值一律判为 False。"""
    return snapshot[column].astype("float64")


def _bool_column(snapshot: pd.DataFrame, column: str) -> "pd.Series[bool]":
    return snapshot[column].astype("bool")


class HighDividendLow:
    """高股息低位策略。"""

    name = "high_dividend_low"

    def __init__(self, strategy_cfg: StrategyConfig, universe_cfg: UniverseConfig) -> None:
        self._cfg = strategy_cfg
        self._universe = universe_cfg

    def buy_candidates(
        self, day: date, snapshot: pd.DataFrame, held: AbstractSet[str]
    ) -> list[BuyCandidate]:
        """按股票池与买入条件筛选，股息率从高到低、同值按代码升序。

        任何指标为空值即不通过（NaN 的比较结果为假）；金融股豁免资产负债率。
        """
        if snapshot.empty:
            return []
        cfg, uni = self._cfg, self._universe
        passes: pd.Series[bool] = (
            ~_bool_column(snapshot, "is_st")
            & (_float_column(snapshot, "listed_years") >= uni.min_listed_years)
            & (_float_column(snapshot, "avg_amount_60d") >= uni.min_avg_amount_60d)
            & (_float_column(snapshot, "dividend_yield") >= cfg.min_dividend_yield)
            & (_float_column(snapshot, "price_position") <= cfg.max_price_position)
            & (_float_column(snapshot, "dividend_years") >= cfg.min_dividend_years)
            & (_float_column(snapshot, "roe_avg3") >= cfg.min_roe_avg3)
            & (_float_column(snapshot, "payout_ratio") <= cfg.max_payout_ratio)
            & (_float_column(snapshot, "profit_yoy") >= cfg.min_profit_yoy)
            & (
                (_float_column(snapshot, "debt_ratio") <= cfg.max_debt_ratio)
                | _bool_column(snapshot, "is_financial")
            )
        )
        symbols = [str(s) for s in snapshot.index[passes.to_numpy()]]
        candidates = [
            self._candidate(day, snapshot, symbol) for symbol in symbols if symbol not in held
        ]
        candidates.sort(key=lambda c: (-c.metrics["dividend_yield"], c.symbol))
        return candidates

    def _candidate(self, day: date, snapshot: pd.DataFrame, symbol: str) -> BuyCandidate:
        metrics = {col: _float_at(snapshot, symbol, col) for col in _METRIC_COLUMNS}
        reason = (
            f"股息率 {metrics['dividend_yield']:.1%}，"
            f"处于一年价格区间底部 {metrics['price_position']:.0%}"
        )
        return BuyCandidate(
            symbol=symbol,
            day=day,
            close=_float_at(snapshot, symbol, "close"),
            industry=_industry_at(snapshot, symbol),
            metrics=metrics,
            reason=reason,
        )

    def sell_signals(
        self, day: date, snapshot: pd.DataFrame, positions: Sequence[Position]
    ) -> list[SellSignal]:
        """逐个持仓按 基本面、止损、止盈、时间止损 的顺序检查，只取第一个命中。

        当日不在快照里（停牌）的持仓不产生信号。
        """
        signals: list[SellSignal] = []
        for pos in positions:
            if pos.symbol not in snapshot.index:
                continue
            hit = self._check(snapshot, pos)
            if hit is not None:
                signals.append(SellSignal(pos.symbol, day, hit[0], hit[1]))
        return signals

    def _check(self, snapshot: pd.DataFrame, pos: Position) -> tuple[ExitReason, str] | None:
        cfg = self._cfg
        symbol = pos.symbol
        ret = pos.ret(_float_at(snapshot, symbol, "close"))

        # 1. 基本面
        if bool(_cell(snapshot, symbol, "dividend_cut")):
            return ExitReason.FUNDAMENTAL, "最近一个财年每股派息低于上一财年，分红被削减"
        profit_yoy = _float_at(snapshot, symbol, "profit_yoy")
        if profit_yoy < cfg.fundamental_profit_yoy:  # NaN 比较为假，不触发
            return (
                ExitReason.FUNDAMENTAL,
                f"净利润同比 {profit_yoy:.1%}，低于基本面止损线 {cfg.fundamental_profit_yoy:.1%}",
            )

        # 2. 价格止损
        if ret <= -cfg.stop_loss:
            return (
                ExitReason.STOP_LOSS,
                f"持仓收益 {ret:.1%}，触及止损线 {-cfg.stop_loss:.1%}",
            )

        # 3. 止盈
        if cfg.take_profit_mode == "fixed":
            if ret >= cfg.take_profit_fixed:
                return (
                    ExitReason.TAKE_PROFIT_FIXED,
                    f"持仓收益 {ret:.1%}，达到止盈线 {cfg.take_profit_fixed:.1%}",
                )
        elif pos.peak_return >= cfg.trailing_activate:
            drawdown = (1 + ret) / (1 + pos.peak_return) - 1
            if drawdown <= -cfg.trailing_drawdown:
                return (
                    ExitReason.TAKE_PROFIT_TRAILING,
                    f"峰值收益 {pos.peak_return:.1%}，当前收益 {ret:.1%}，"
                    f"自峰值回落 {-drawdown:.1%}，达到回落止盈线 {cfg.trailing_drawdown:.1%}",
                )

        # 4. 时间止损
        if pos.days_held >= cfg.time_stop_days and ret <= 0:
            return (
                ExitReason.TIME_STOP,
                f"持仓 {pos.days_held} 天（上限 {cfg.time_stop_days} 天）仍未盈利，"
                f"当前收益 {ret:.1%}",
            )
        return None
