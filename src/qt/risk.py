"""风控层：回撤预警与暂停、卖出后冷却、仓位与行业集中度控制。

本模块不依赖回测引擎，回测与将来的实盘共用。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from qt.calendar import TradingCalendar
from qt.config import RiskConfig
from qt.costs import CostModel
from qt.models import BuyCandidate, ExitReason, Order, Position, Side

_LOT = 100
_EPS = 1e-9
# 触发冷却的卖出原因；止盈与退市不冷却
_COOLING_REASONS = frozenset({ExitReason.STOP_LOSS, ExitReason.TIME_STOP, ExitReason.FUNDAMENTAL})


class RiskEventKind(StrEnum):
    """风控事件类型。"""

    WARN = "warn"
    PAUSE = "pause"
    RESUME = "resume"


@dataclass(frozen=True)
class RiskEvent:
    """风控事件。drawdown 为负数（如 -0.2 表示回撤 20%）。"""

    day: date
    kind: RiskEventKind
    drawdown: float
    message: str


@dataclass(frozen=True)
class Rejection:
    """被拒绝的买入候选。

    rule 取值：paused、cooldown、max_positions、industry_cap、lot_too_expensive、no_cash。
    """

    candidate: BuyCandidate
    rule: str
    message: str


class RiskManager:
    """风控管理器：持有回撤状态机与冷却记录。"""

    def __init__(self, cfg: RiskConfig, calendar: TradingCalendar, costs: CostModel) -> None:
        self._cfg = cfg
        self._calendar = calendar
        self._costs = costs
        self._peak: float | None = None
        self._warned = False
        self._paused = False
        self._pause_day: date | None = None
        self._pause_days = cfg.pause_days
        self._exits: dict[str, date] = {}

    def buying_allowed(self) -> bool:
        """是否允许买入（暂停期间为假）。"""
        return not self._paused

    def _reset_after_resume(self, day: date, nav: float) -> RiskEvent:
        """恢复买入：峰值重置为当日净值，重新武装 WARN。"""
        assert self._peak is not None
        drawdown = nav / self._peak - 1
        self._paused = False
        self._pause_day = None
        self._pause_days = self._cfg.pause_days
        self._peak = nav
        self._warned = False
        return RiskEvent(
            day,
            RiskEventKind.RESUME,
            drawdown,
            f"恢复买入：暂停期结束，当日净值 {nav:.4f}，历史最高净值重置为 {nav:.4f}",
        )

    def on_day_close(self, day: date, nav: float) -> list[RiskEvent]:
        """收盘后更新回撤状态，返回当日产生的事件。nav 为单位净值。"""
        if self._paused:
            assert self._pause_day is not None
            if self._calendar.count_between(self._pause_day, day) >= self._pause_days:
                return [self._reset_after_resume(day, nav)]
            return []
        if self._peak is None or nav > self._peak:
            self._peak = nav
            self._warned = False
        drawdown = nav / self._peak - 1
        # 浮点容差：0.8/1.0-1 不应因舍入而漏掉恰好 -20% 的情形
        if drawdown <= -self._cfg.pause_drawdown + _EPS:
            self._paused = True
            self._pause_day = day
            self._pause_days = self._cfg.pause_days
            self._warned = True
            return [
                RiskEvent(
                    day,
                    RiskEventKind.PAUSE,
                    drawdown,
                    f"暂停买入：回撤 {drawdown:.2%} 已达暂停线 -{self._cfg.pause_drawdown:.0%}，"
                    f"净值 {nav:.4f}，历史最高 {self._peak:.4f}，"
                    f"暂停 {self._pause_days} 个交易日",
                )
            ]
        if drawdown <= -self._cfg.warn_drawdown + _EPS and not self._warned:
            self._warned = True
            return [
                RiskEvent(
                    day,
                    RiskEventKind.WARN,
                    drawdown,
                    f"回撤预警：回撤 {drawdown:.2%} 已达预警线 -{self._cfg.warn_drawdown:.0%}，"
                    f"净值 {nav:.4f}，历史最高 {self._peak:.4f}",
                )
            ]
        return []

    def resume(self, day: date, nav: float) -> RiskEvent:
        """手动提前恢复买入；未暂停时抛 ValueError。"""
        if not self._paused:
            raise ValueError("当前未处于暂停状态，无需恢复")
        return self._reset_after_resume(day, nav)

    def extend_pause(self, days: int) -> None:
        """手动延长暂停天数（交易日）。"""
        self._pause_days += days

    def note_exit(self, symbol: str, day: date, reason: ExitReason) -> None:
        """记录卖出；止损、时间止损、基本面恶化会触发冷却。"""
        if reason in _COOLING_REASONS:
            self._exits[symbol] = day

    def _in_cooldown(self, symbol: str, day: date) -> int | None:
        """冷却中返回已过交易日数，否则返回 None。"""
        exit_day = self._exits.get(symbol)
        if exit_day is None:
            return None
        elapsed = self._calendar.count_between(exit_day, day)
        return elapsed if elapsed < self._cfg.cooldown_days else None

    def size_buys(
        self,
        day: date,
        candidates: Sequence[BuyCandidate],
        equity: float,
        cash: float,
        positions: Sequence[Position],
        closes: Mapping[str, float],
        pending_buys: Sequence[Order] = (),
    ) -> tuple[list[Order], list[Rejection]]:
        """按候选顺序逐个定仓，返回 (通过的买单, 被拒记录)。

        待成交买单只计入持仓数与现金占用（Order 不含行业，无法计入行业市值）。
        """
        orders: list[Order] = []
        rejections: list[Rejection] = []

        industry_mv: dict[str, float] = {}
        for p in positions:
            value = p.shares * closes[p.symbol] if p.symbol in closes else p.cost
            industry_mv[p.industry] = industry_mv.get(p.industry, 0.0) + value
        count = len(positions) + len(pending_buys)
        free_cash = cash
        for o in pending_buys:
            if o.symbol in closes:
                free_cash -= self._costs.buy_cost(closes[o.symbol], o.shares)

        budget = equity * self._cfg.max_position_pct
        industry_limit = equity * self._cfg.max_industry_pct

        for c in candidates:
            rule, message, shares = self._decide(
                c, day, closes, budget, industry_limit, industry_mv, free_cash, count
            )
            if rule is not None:
                rejections.append(Rejection(c, rule, message))
                continue
            price = closes.get(c.symbol, c.close)
            orders.append(Order(c.symbol, Side.BUY, shares, day, c.reason, None))
            count += 1
            free_cash -= self._costs.buy_cost(price, shares)
            industry_mv[c.industry] = industry_mv.get(c.industry, 0.0) + shares * price
        return orders, rejections

    def _decide(
        self,
        c: BuyCandidate,
        day: date,
        closes: Mapping[str, float],
        budget: float,
        industry_limit: float,
        industry_mv: Mapping[str, float],
        free_cash: float,
        count: int,
    ) -> tuple[str | None, str, int]:
        """对单个候选依次检查各规则，返回 (拒绝规则或 None, 说明, 股数)。"""
        if not self.buying_allowed():
            return "paused", "暂停买入期间不开新仓", 0
        elapsed = self._in_cooldown(c.symbol, day)
        if elapsed is not None:
            return (
                "cooldown",
                f"{c.symbol} 卖出后冷却中：已过 {elapsed} 个交易日，"
                f"需满 {self._cfg.cooldown_days} 个",
                0,
            )
        if count >= self._cfg.max_positions:
            return (
                "max_positions",
                f"持仓数（含待成交买单）已达上限 {self._cfg.max_positions} 只",
                0,
            )
        price = closes.get(c.symbol, c.close)
        budget_lots = self._costs.max_lots(price, budget)
        if budget_lots == 0:
            return (
                "lot_too_expensive",
                f"一手需约 {price * _LOT:.2f} 元（价格 {price:.2f}），超过单只预算 {budget:.2f} 元",
                0,
            )
        existing = industry_mv.get(c.industry, 0.0)
        room = industry_limit - existing
        industry_lots = int(room / (price * _LOT) + _EPS) if room > 0 else 0
        if industry_lots == 0:
            return (
                "industry_cap",
                f"行业 {c.industry} 已持有市值 {existing:.2f} 元，"
                f"上限 {industry_limit:.2f} 元，剩余额度买不起一手",
                0,
            )
        cash_lots = self._costs.max_lots(price, free_cash)
        if cash_lots == 0:
            return "no_cash", f"可用现金 {free_cash:.2f} 元不足以买入一手（价格 {price:.2f}）", 0
        lots = min(budget_lots, industry_lots, cash_lots)
        return None, "", lots * _LOT
