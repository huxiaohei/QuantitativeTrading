"""定时进程：每 60 秒醒一次，到点或休眠醒来发现已过点就补跑；数据失败每 30 分钟重试，超时告警。

不用一次性长睡眠：电脑休眠恢复后，下一个 60 秒节拍就能发现错过了运行时刻。
"""

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.data.update import DataIntegrityError, latest_trading_day, load_calendar
from qt.jobs.daily import (
    DATA_FAILED,
    MARKET_TZ,
    RunContext,
    alert,
    catch_up,
    describe,
    due_day,
    first_start_day,
    has_pending_mail,
    parse_clock,
    pending_days,
    run_deadline_at,
    send_outbox,
)

_log = logging.getLogger(__name__)

TICK_SECONDS = 60
RETRY_INTERVAL = timedelta(minutes=30)  # 数据失败后的重试间隔，也是发件箱补发的间隔
_MAX_LOOKBACK_DAYS = 31  # 往回找最近一个交易日运行时刻的上限


def is_due(
    now: datetime,
    run_time: str,
    last_attempt: datetime | None,
    trading_day: Callable[[date], bool] | None = None,
) -> bool:
    """是否该跑每日任务：最近一个（交易日的）运行时刻已过，且在那之后还没尝试过。

    从没尝试过（进程刚启动）时只看今天：今天是交易日且已过 run_time。trading_day 不给时
    不区分交易日（每天都有运行时刻）。休眠跨过了运行时刻的，醒来后立即为真。
    """
    local = now.astimezone(MARKET_TZ)
    at = parse_clock(run_time)

    def moment(d: date) -> datetime:
        return datetime.combine(d, at, tzinfo=MARKET_TZ)

    def trading(d: date) -> bool:
        return trading_day is None or trading_day(d)

    if last_attempt is None:
        return local >= moment(local.date()) and trading(local.date())
    d = local.date() if local >= moment(local.date()) else local.date() - timedelta(days=1)
    for _ in range(_MAX_LOOKBACK_DAYS):
        if moment(d) <= last_attempt:
            return False
        if trading(d):
            return True
        d -= timedelta(days=1)
    return False


def _calendar(ctx: RunContext) -> TradingCalendar | None:
    try:
        return load_calendar(ctx.store)
    except Exception:  # 日历文件缺失或损坏：本节拍不判断交易日，下次再读
        _log.exception("读取交易日历失败")
        return None


class _Worker:
    """run_forever 的状态：上次尝试时间、下次重试时间、已确认过期限的交易日。"""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.last_attempt: datetime | None = None
        self.retry_at: datetime | None = None
        self.retry_until: datetime | None = None
        self.settled: set[date] = set()  # 已完成或已发过超时告警的交易日
        self.last_flush: datetime | None = None

    def attempt(self, now: datetime) -> None:
        """补跑一次。先排好重试，成功（没有 data_failed、没有异常）才取消，告警出错也不影响重试。"""
        self.last_attempt = now
        self.retry_at = now + RETRY_INTERVAL
        self.retry_until = self._retry_until(now)
        try:
            results = catch_up(self.ctx)
        except Exception as exc:
            _log.exception("每日运行出错")
            self._report(now, exc)
            return
        if not any(status == DATA_FAILED for _, status in results):
            self.retry_at = None
            self.retry_until = None

    def _retry_until(self, now: datetime) -> datetime | None:
        """重试截止：当晚要跑的交易日的完成期限（次日 run_deadline）。

        已过期限（如休眠醒来时）或读不到日历时为 None：一直每 30 分钟重试，直到下一次到点。
        """
        calendar = _calendar(self.ctx)
        if calendar is None:
            return None
        service = self.ctx.cfg.service
        try:
            night = due_day(now, calendar, service.run_time)
        except (DataIntegrityError, ValueError):
            return None
        until = run_deadline_at(night, service.run_deadline)
        return until if until > now else None

    def _report(self, now: datetime, exc: Exception) -> None:
        """意外错误告警；告警本身失败（如数据库暂时不可用）只记日志。"""
        local_day = now.astimezone(MARKET_TZ).date()
        try:
            alert(
                self.ctx,
                local_day,
                "每日运行出错",
                ["补跑过程意外中断，将稍后重试。", describe(exc)],
            )
            send_outbox(self.ctx, local_day)
        except Exception:
            _log.exception("每日运行出错的告警没有发出")

    def tick(self, now: datetime) -> None:
        calendar = _calendar(self.ctx)
        trading = calendar.is_trading_day if calendar is not None else None
        service = self.ctx.cfg.service
        if is_due(now, service.run_time, self.last_attempt, trading) or (
            self.retry_at is not None
            and now >= self.retry_at
            and (self.retry_until is None or now < self.retry_until)
        ):
            self.attempt(now)
        if calendar is not None:
            self.check_deadline(now, calendar)
        self.flush_outbox(now)

    def check_deadline(self, now: datetime, calendar: TradingCalendar) -> None:
        """期限已过的最近一个交易日还没跑完：给管理员发一次超时告警（每个交易日一次）。"""
        service = self.ctx.cfg.service
        local = now.astimezone(MARKET_TZ)
        back = 1 if local.time() >= parse_clock(service.run_deadline) else 2
        try:
            day = latest_trading_day(calendar, local.date() - timedelta(days=back))
        except DataIntegrityError:
            return
        if day in self.settled:
            return
        with Session(self.ctx.engine) as s:
            pending = day in pending_days(s, calendar, day, first_start_day(s))
        self.settled.add(day)
        if not pending:
            return
        deadline = run_deadline_at(day, service.run_deadline)
        alert(
            self.ctx,
            day,
            "每日运行超时",
            [f"交易日 {day} 的每日运行到 {deadline:%Y-%m-%d %H:%M} 仍未完成，本日没有发出信号。"],
        )
        send_outbox(self.ctx, day)

    def flush_outbox(self, now: datetime) -> None:
        """每 30 分钟补发一次发件箱里的待发邮件（SMTP 曾经失败的）。"""
        if self.last_flush is not None and now - self.last_flush < RETRY_INTERVAL:
            return
        self.last_flush = now
        if has_pending_mail(self.ctx):
            send_outbox(self.ctx, now.astimezone(MARKET_TZ).date())


def run_forever(ctx: RunContext, sleep: Callable[[float], None] = time.sleep) -> None:
    """常驻运行：启动时补跑一次，之后每 60 秒检查一次。单个节拍出错只记日志，不退出。"""
    worker = _Worker(ctx)
    _log.info("定时进程启动")
    try:
        worker.attempt(ctx.now())
    except Exception:
        _log.exception("启动时补跑出错，下个节拍继续")
    while True:
        sleep(TICK_SECONDS)
        try:
            worker.tick(ctx.now())
        except Exception:
            _log.exception("定时检查出错，下个节拍继续")
