"""各页面共用的展示口径：中文标签、订阅的展示与解析。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy.orm import Session

from qt.db import repo
from qt.db.models import Subscription
from qt.service import rules

BookKind = Literal["manual", "system"]
BOOKS: tuple[BookKind, ...] = ("manual", "system")
Names = Mapping[str, str]  # 股票代码 -> 名称

BOOK_LABELS: Mapping[str, str] = {"manual": "手动账", "system": "系统账"}
SUB_STATUS_LABELS: Mapping[str, str] = {"active": "生效中", "paused": "已暂停", "closed": "已关闭"}
KIND_LABELS: Mapping[str, str] = {
    "buy": "买入",
    "sell": "卖出",
    "transfer_in": "转入持仓",
    "deposit": "入金",
    "withdraw": "出金",
    "void": "作废流水",
    "dividend": "分红（推算）",
    "bonus": "送转（推算）",
}
SIGNAL_STATUS_LABELS: Mapping[str, str] = {
    "created": "已生成",
    "notified": "已通知",
    "executed": "已执行",
    "abandoned": "已放弃",
    "voided": "已作废",
    "unnotified": "未通知（补跑）",
}
SIDE_LABELS: Mapping[str, str] = {"buy": "买入信号", "sell": "卖出信号"}
EXIT_LABELS: Mapping[str, str] = {
    "take_profit_fixed": "固定止盈",
    "take_profit_trailing": "移动止盈",
    "stop_loss": "止损",
    "time_stop": "持有期满",
    "fundamental": "基本面",
    "delisted": "退市",
    "manual": "自主卖出",
}
FOLLOWUP_LABELS: Mapping[str, str] = {
    "exit": "离场后",
    "abandoned": "放弃买入后",
    "rejected": "被规则否决后",
}
RUN_STATUS_LABELS: Mapping[str, str] = {
    "running": "运行中（或中途中断）",
    "done": "完成",
    "done_with_errors": "完成，但有使用者处理出错",
    "data_failed": "数据异常，本日未出信号",
}


def origin_label(origin: str | None, side: str = "买入") -> str:
    """来源：按信号买入 / 自主买入（side 可为"卖出"等）。"""
    return f"按信号{side}" if origin == "signal" else f"自主{side}"


def name_of(names: Names | None, symbol: str | None) -> str:
    """股票名称；不知道时为空字符串。"""
    if names is None or symbol is None:
        return ""
    return names.get(symbol, "")


@dataclass(frozen=True)
class SubscriptionView:
    """一个订阅的展示信息。read_only 为真表示已关闭，页面只能查看。"""

    id: int
    status: str
    status_label: str
    initial_cash: float
    start_day: date
    created_at: datetime
    closed_at: datetime | None
    read_only: bool


def subscription_view(sub: Subscription) -> SubscriptionView:
    return SubscriptionView(
        id=sub.id,
        status=sub.status,
        status_label=SUB_STATUS_LABELS.get(sub.status, sub.status),
        initial_cash=sub.initial_cash,
        start_day=sub.start_day,
        created_at=sub.created_at,
        closed_at=sub.closed_at,
        read_only=sub.status == "closed",
    )


def resolve_subscription(
    s: Session, user_id: int, subscription_id: int | None
) -> Subscription | None:
    """页面要看的订阅：给了编号就取该使用者名下的这一个（不是本人的抛 NotFoundError）；

    没给就按查看口径（rules.shown_subscription）取；一个订阅都没有为 None。
    """
    if subscription_id is None:
        return rules.shown_subscription(s, user_id)
    return repo.get_subscription(s, user_id, subscription_id)


def subscriptions(s: Session, user_id: int) -> list[SubscriptionView]:
    """该使用者的全部订阅（含已关闭的），按创建时间倒序。"""
    return [subscription_view(sub) for sub in rules.list_subscriptions(s, user_id)]
