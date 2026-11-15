"""领域对象：信号、持仓、订单与成交记录。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Side(StrEnum):
    """买卖方向。"""

    BUY = "buy"
    SELL = "sell"


class ExitReason(StrEnum):
    """卖出原因。"""

    TAKE_PROFIT_FIXED = "take_profit_fixed"
    TAKE_PROFIT_TRAILING = "take_profit_trailing"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    FUNDAMENTAL = "fundamental"
    DELISTED = "delisted"


@dataclass(frozen=True)
class BuyCandidate:
    """买入候选。reason 是给人看的中文说明。"""

    symbol: str
    day: date
    close: float
    industry: str
    metrics: Mapping[str, float]
    reason: str


@dataclass(frozen=True)
class SellSignal:
    """卖出信号。"""

    symbol: str
    day: date
    reason: ExitReason
    detail: str


@dataclass
class Position:
    """持仓。cost 为含费用的总买入金额。

    cash_received 为持有期间收到的现金分红（只有手动账使用；系统账按再投资折算股数，恒为 0）。
    """

    symbol: str
    shares: float
    cost: float
    entry_day: date
    industry: str
    entry_metrics: Mapping[str, float]
    days_held: int = 0
    peak_return: float = 0.0
    cash_received: float = 0.0

    def ret(self, close: float) -> float:
        """按收盘价计算的持仓收益率：(市值 + 已收现金分红) / 成本 - 1。"""
        return (self.shares * close + self.cash_received) / self.cost - 1

    def scale_shares(self, factor_ratio: float) -> None:
        """复权因子变化时按 当日因子/前一日因子 放大股数，等同分红再投资。"""
        self.shares *= factor_ratio


@dataclass(frozen=True)
class Order:
    """订单。"""

    symbol: str
    side: Side
    shares: float
    signal_day: date
    reason: str
    exit_reason: ExitReason | None = None


@dataclass(frozen=True)
class Fill:
    """一笔成交（买入或卖出），撮合成功与退市清算时记录。"""

    symbol: str
    side: Side
    day: date
    shares: float
    price: float  # 成交价（含滑点）
    amount: float  # 买入为含费用的支出，卖出为扣费后的收入
    signal_day: date  # 退市清算没有信号，记为清算当天


@dataclass(frozen=True)
class Trade:
    """一笔已平仓交易。cost 为含费用买入总额，proceeds 为扣费后卖出净额。"""

    symbol: str
    entry_day: date
    exit_day: date
    cost: float
    proceeds: float
    exit_reason: ExitReason
    holding_days: int
    entry_metrics: Mapping[str, float]

    @property
    def pnl(self) -> float:
        """盈亏金额。"""
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        """收益率。"""
        return self.proceeds / self.cost - 1
