"""信号与被拒候选的记录：系统账与手动账共用。

本模块的函数只 flush、不 commit，事务边界由调用方决定。
"""

import math
from collections.abc import Mapping
from datetime import date
from enum import StrEnum

from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.db import repo
from qt.db.models import Book, Signal
from qt.db.models import Rejection as RejectionRow
from qt.models import ExitReason, Side
from qt.risk import Rejection
from qt.service.errors import ValidationError


class SignalStatus(StrEnum):
    """信号状态（见设计文档 7.7）。"""

    CREATED = "created"  # 当晚运行产生
    NOTIFIED = "notified"  # 邮件已发出
    EXECUTED = "executed"  # 系统账次日成交；手动账录入了关联成交
    ABANDONED = "abandoned"  # 手动账：有效期内没有录入成交
    VOIDED = "voided"  # 系统账买单超过有效期未成交，或暂停买入时被取消
    UNNOTIFIED = "unnotified"  # 补跑产生的过期信号


# 已经结束的状态不再改变
_FINISHED = frozenset({SignalStatus.EXECUTED, SignalStatus.ABANDONED, SignalStatus.VOIDED})


def metrics_json(metrics: Mapping[str, float]) -> dict[str, float | None]:
    """指标转成可存入 JSONB 的对象：NaN 与无穷大（JSON 不支持）记为 null，其余原样不取整。"""
    return {k: (v if math.isfinite(v) else None) for k, v in metrics.items()}


def create_signal(
    s: Session,
    book: Book,
    side: Side,
    symbol: str,
    day: date,
    shares: float,
    ref_price: float,
    reason: str,
    exit_reason: ExitReason | None,
    metrics: Mapping[str, float],
    valid_until: date | None,
) -> Signal:
    """新建一条状态为"已生成"的信号；ref_price 为信号日收盘价。"""
    signal = Signal(
        book_id=book.id,
        user_id=book.user_id,
        side=side.value,
        symbol=symbol,
        signal_day=day,
        shares=shares,
        ref_price=ref_price,
        reason=reason,
        exit_reason=None if exit_reason is None else exit_reason.value,
        metrics=metrics_json(metrics),
        status=SignalStatus.CREATED.value,
        status_day=day,
        valid_until=valid_until,
    )
    s.add(signal)
    s.flush()
    return signal


def mark_signal(
    s: Session, signal: Signal, status: SignalStatus, day: date, note: str | None = None
) -> None:
    """改变信号状态并记下发生日；note 写入 void_note。已执行、已放弃、已作废的信号不能再改。"""
    if SignalStatus(signal.status) in _FINISHED:
        raise ValidationError(
            f"信号 {signal.id} 已是 {signal.status} 状态，不能改为 {status.value}"
        )
    signal.status = status.value
    signal.status_day = day
    if note is not None:
        signal.void_note = note
    s.flush()


def record_rejection(
    s: Session,
    book: Book,
    rejection: Rejection,
    day: date,
    calendar: TradingCalendar,
    gap_days: int,
) -> RejectionRow:
    """记录一次被拒候选，连续出现算一次。

    同一账本、同一股票、同一规则最近一条记录的 last_day 距 day 不超过 gap_days 个交易日时，
    只把它的 last_day 推到 day；否则新建一条（first_day = last_day = day）。
    """
    symbol = rejection.candidate.symbol
    latest = repo.latest_rejection(s, book.user_id, book.id, symbol, rejection.rule)
    if latest is not None and calendar.count_between(latest.last_day, day) <= gap_days:
        latest.last_day = max(latest.last_day, day)
        s.flush()
        return latest
    row = RejectionRow(
        book_id=book.id,
        user_id=book.user_id,
        symbol=symbol,
        rule=rejection.rule,
        message=rejection.message,
        metrics=metrics_json(rejection.candidate.metrics),
        first_day=day,
        last_day=day,
    )
    s.add(row)
    s.flush()
    return row
