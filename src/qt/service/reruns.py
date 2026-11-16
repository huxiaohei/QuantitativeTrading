"""管理员在网页上提出的"立即重试"请求：登记、由 worker 取走处理、写回结果。

请求只有两个状态：未处理（handled_at 为空）与已处理（写了处理时间与结果）。worker 取走请求
并不改状态，处理完才 finish；worker 中途退出时请求仍是未处理，重启后会再被取到。
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.data.update import DataIntegrityError
from qt.db import repo
from qt.db.models import RerunRequest
from qt.jobs.daily import due_pending
from qt.service.actor import web_actor


def has_unfinished_day(s: Session, calendar: TradingCalendar, now: datetime, run_time: str) -> bool:
    """存在已到运行时间、但还没有 done 记录的交易日。

    判断口径与夜间补跑（catch_up）选日子的口径是同一个函数 daily.due_pending。
    交易日历里排不出"最近一个已收盘的交易日"时无从判断，按没有未完成的日子处理。
    """
    try:
        _, days = due_pending(s, calendar, now, run_time)
    except (DataIntegrityError, ValueError):
        return False
    return bool(days)


def _oldest_pending(s: Session) -> RerunRequest | None:
    stmt = (
        select(RerunRequest)
        .where(RerunRequest.handled_at.is_(None))
        .order_by(RerunRequest.id)
        .limit(1)
    )
    return s.scalars(stmt).first()


def request(s: Session, admin_id: int, now: datetime) -> RerunRequest:
    """登记一次重试请求；已有未处理的请求时返回它，不再新建。新建时写审计。只 flush。"""
    existing = _oldest_pending(s)
    if existing is not None:
        return existing
    row = RerunRequest(requested_by=admin_id, requested_at=now)
    s.add(row)
    s.flush()
    repo.add_audit(s, None, "rerun_request", f"rerun:{row.id}", {}, web_actor(admin_id))
    return row


def has_pending(s: Session) -> bool:
    """是否有还没处理完的重试请求（worker 正在处理或等待处理）。"""
    return _oldest_pending(s) is not None


def take_pending(s: Session) -> RerunRequest | None:
    """最早的一条未处理请求；没有返回 None。不改状态，处理完要调用 finish。"""
    return _oldest_pending(s)


def finish(s: Session, request_id: int, result: str, now: datetime) -> None:
    """记下处理时间与结果（一行文字）。只 flush。"""
    row = s.get(RerunRequest, request_id, with_for_update=True)
    if row is None:
        return
    row.handled_at = now
    row.result = result
    s.flush()


def latest(s: Session) -> RerunRequest | None:
    """最近一次重试请求（不论是否处理过）；没有返回 None。"""
    return s.scalars(select(RerunRequest).order_by(RerunRequest.id.desc()).limit(1)).first()


def day_results(results: list[tuple[date, str]]) -> str:
    """把补跑的各日结果拼成一行：2026-11-16 done；2026-11-17 data_failed。"""
    if not results:
        return "没有需要处理的交易日"
    return "；".join(f"{day} {status}" for day, status in results)


def status_text(row: RerunRequest | None) -> str:
    """最近一次重试请求的状态，一行中文（运行记录页用）。"""
    if row is None:
        return "没有重试请求"
    asked = f"{row.requested_at:%Y-%m-%d %H:%M}"
    if row.handled_at is None:
        return f"{asked} 提出的请求：等待 worker 处理"
    return f"{asked} 提出的请求：已于 {row.handled_at:%H:%M} 处理，结果：{row.result or '无'}"
