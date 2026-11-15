"""每日任务：更新数据、检查、构建面板，逐个使用者推进两本账并写邮件，最后统一发送；以及补跑。

阶段（daily_runs.stage）依次为 data、check、panel、users、mail、done。数据更新或完整性检查失败时
状态为 data_failed，本日不给任何人出信号，并告警管理员。每个使用者在自己的事务里处理：出任何异常
都整体回滚该使用者当晚的全部写入，记入 detail 并告警，继续处理下一个。重跑同一天不会产生重复：
已推进的账本靠 books.processed_through 跳过，邮件靠发件箱的去重键。所有"现在"都取 ctx.now()。
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    DataIntegrityError,
    SharedTableError,
    check_integrity,
    latest_closed_day,
    load_calendar,
    refresh_fundamentals,
    update_all,
)
from qt.db import admin_repo, repo
from qt.db.models import DailyRun, Outbox, Subscription
from qt.features import FeaturePanel, build_panel
from qt.jobs.mails import (
    alert_admin,
    is_last_trading_day_of_week,
    queue_signal_mails,
    queue_weekly,
    security_names,
)
from qt.mail.outbox import send_pending
from qt.mail.send import Sender
from qt.service.accounts import subscription_config
from qt.service.journal import fill_followups, open_followups
from qt.service.manual_ops import advance_manual_book, manual_state
from qt.service.market import StoreMarket
from qt.service.system_book import advance_system_book
from qt.strategy.high_dividend_low import HighDividendLow

_log = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("Asia/Shanghai")
RUNNING = "running"
DATA_FAILED = "data_failed"
DONE = "done"
DONE_WITH_ERRORS = "done_with_errors"
FINISHED = (DONE, DONE_WITH_ERRORS)
_MAX_ERROR_CHARS = 500
_LOOKBACK_SLACK_DAYS = 5  # 跟踪记录回看窗口 = 补记上限 + 这几天


@dataclass(frozen=True)
class RunContext:
    """每日任务需要的一切外部资源；now 返回带时区的当前时间（测试里换成假时钟）。"""

    engine: Engine
    store: ParquetStore
    source: DataSource
    cfg: AppConfig
    sender: Sender
    admin_email: str
    now: Callable[[], datetime]


def describe(exc: BaseException) -> str:
    """异常的简短文字：类型加消息，限长。"""
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


def _session(ctx: RunContext) -> Session:
    return Session(ctx.engine, expire_on_commit=False)


def parse_clock(text: str) -> time:
    """ "HH:MM" -> time。"""
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


# ---------- 运行记录、告警与发送 ----------


class _Run:
    """daily_runs 里一天的记录；每次改动都在自己的短事务里提交。"""

    def __init__(self, ctx: RunContext, day: date) -> None:
        self.ctx = ctx
        self.day = day
        self.detail: dict[str, Any] = {}
        with _session(ctx) as s:
            row = s.get(DailyRun, day)
            if row is None:
                row = DailyRun(trade_day=day, detail={})
                s.add(row)
            row.status = RUNNING
            row.stage = "data"
            row.started_at = ctx.now()
            row.finished_at = None
            row.detail = {}
            s.commit()

    def stage(self, name: str) -> None:
        self._write(RUNNING, name, None)

    def finish(self, status: str, stage: str) -> str:
        self._write(status, stage, self.ctx.now())
        return status

    def _write(self, status: str, stage: str, finished_at: datetime | None) -> None:
        with _session(self.ctx) as s:
            row = s.get(DailyRun, self.day)
            if row is None:
                raise RuntimeError(f"daily_runs 缺少 {self.day} 的记录")
            row.status = status
            row.stage = stage
            row.finished_at = finished_at
            row.detail = dict(self.detail)
            s.commit()


def alert(ctx: RunContext, day: date, title: str, lines: Sequence[str]) -> None:
    """管理员告警入队（同一天同一标题只一次），立即提交。"""
    _log.warning("告警 %s %s：%s", day, title, "；".join(lines))
    with _session(ctx) as s:
        alert_admin(s, ctx.admin_email, day, title, lines)
        s.commit()


def _failed_ids(s: Session) -> set[int]:
    return set(s.scalars(select(Outbox.id).where(Outbox.status == "failed")))


def send_outbox(ctx: RunContext, day: date) -> tuple[int, int]:
    """发送发件箱里的待发邮件；本次有邮件变为发送失败（不再重试）时告警管理员。

    用独立会话：send_pending 逐封提交，不能夹带别的未提交工作。返回（成功数，失败次数）。
    """
    with _session(ctx) as s:
        before = _failed_ids(s)
        sent, failed = send_pending(s, ctx.sender)
        dead = [
            row
            for row in s.scalars(
                select(Outbox).where(Outbox.status == "failed").order_by(Outbox.id)
            )
            if row.id not in before
        ]
        if dead:
            lines = [
                f"邮件 #{row.id}（{row.kind}，收件人 {row.to_addr}）"
                f"已尝试 {row.attempts} 次仍失败：{row.last_error or ''}"
                for row in dead
            ]
            if alert_admin(s, ctx.admin_email, day, "邮件发送失败", lines):
                more_sent, more_failed = send_pending(s, ctx.sender)
                sent, failed = sent + more_sent, failed + more_failed
        s.commit()
    return sent, failed


def has_pending_mail(ctx: RunContext) -> bool:
    """发件箱里有没有待发邮件。"""
    with _session(ctx) as s:
        stmt = select(func.count()).select_from(Outbox).where(Outbox.status == "pending")
        return bool(s.scalar(stmt))


def _send_quietly(ctx: RunContext, day: date, run: "_Run | None" = None) -> None:
    """发送失败（包括数据库出错）不改变运行状态，记日志与 detail，邮件留待下次重试。"""
    try:
        sent, failed = send_outbox(ctx, day)
    except Exception as exc:
        _log.exception("发送邮件出错")
        if run is not None:
            run.detail["mail_error"] = describe(exc)
        return
    if run is not None:
        run.detail["mail"] = {"sent": sent, "failed": failed}


# ---------- 数据 ----------


def _refresh_trade_days(ctx: RunContext) -> None:
    try:
        ctx.store.write(Table.TRADE_DAYS, ctx.source.trade_days())
    except Exception as exc:
        raise SharedTableError(f"trade_days 更新失败：{describe(exc)}") from exc


def _progress(stage: str, done: int, total: int) -> None:
    _log.info("数据更新 %s 已处理 %d/%d 只股票", stage, done, total)


def _update_data(ctx: RunContext, as_of: date) -> list[str]:
    """增量更新到 as_of（交易日历须已刷新），返回需要视为数据问题的描述。"""
    report = update_all(
        ctx.source, ctx.store, ctx.cfg, as_of, progress=_progress, refresh_trade_days=False
    )
    _log.info(
        "数据更新完成：更新 %d 只，跳过 %d 只，失败 %d 只",
        report.updated,
        report.skipped,
        len(report.failed),
    )
    return list(report.problems)


def _data_failed(
    ctx: RunContext, run: _Run, stage: str, title: str, detail: dict[str, Any], lines: Sequence[str]
) -> str:
    run.detail.update(detail)
    run.finish(DATA_FAILED, stage)
    alert(ctx, run.day, title, [f"本日没有给任何使用者出信号。阶段：{stage}", *lines])
    _send_quietly(ctx, run.day)
    return DATA_FAILED


def _stored_calendar(store: ParquetStore) -> TradingCalendar | None:
    try:
        return load_calendar(store)
    except DataIntegrityError:
        return None


# ---------- 面板与基本面复核 ----------


class _Panels:
    """本次运行共用的指标面板：当日面板一份；落后的账本需要更早的日子时另建一份更宽的。"""

    def __init__(self, ctx: RunContext, calendar: TradingCalendar, day: date) -> None:
        self.ctx = ctx
        self.calendar = calendar
        self.day = day
        self.panel = build_panel(ctx.store, calendar, ctx.cfg, day, day)
        self.market = StoreMarket(ctx.store, self.panel, ctx.cfg)
        self._wide: tuple[date, FeaturePanel, StoreMarket] | None = None
        self._checked: dict[date, list[str]] = {}

    def rebuild(self) -> None:
        self.panel = build_panel(self.ctx.store, self.calendar, self.ctx.cfg, self.day, self.day)
        self.market = StoreMarket(self.ctx.store, self.panel, self.ctx.cfg)
        self._wide = None

    def for_day(self, d: date) -> tuple[FeaturePanel, StoreMarket]:
        if d == self.day:
            return self.panel, self.market
        if self._wide is None or self._wide[0] > d:
            panel = build_panel(self.ctx.store, self.calendar, self.ctx.cfg, d, self.day)
            self._wide = (d, panel, StoreMarket(self.ctx.store, panel, self.ctx.cfg))
        return self._wide[1], self._wide[2]

    def require_complete(self, d: date) -> None:
        """补推进更早的日子前，确认那天的数据也完整。"""
        if d not in self._checked:
            self._checked[d] = check_integrity(self.ctx.store, self.calendar, d)
        if self._checked[d]:
            raise DataIntegrityError(f"{d} 的数据不完整，无法补推进：{'；'.join(self._checked[d])}")


def _due_subscriptions(s: Session, day: date) -> list[Subscription]:
    """当天要处理的订阅：生效中且起始日不晚于 day（暂停、关闭、尚未开始的跳过）。"""
    return [sub for sub in admin_repo.list_active_subscriptions(s) if sub.start_day <= day]


def _holdings(
    s: Session, sub: Subscription, cfg: AppConfig, panels: _Panels, day: date
) -> set[str]:
    """订阅两本账当前持有的股票。"""
    held: set[str] = set()
    system = repo.get_book_of_kind(s, sub.user_id, sub.id, "system")
    row = repo.get_system_state(s, sub.user_id, system.id)
    if row is not None:
        held |= set(load_state(row.state)[0].positions)
    state = manual_state(s, sub.user_id, sub.id, panels.market, panels.calendar, cfg, day)
    return held | set(state.positions)


def _recheck_symbols(ctx: RunContext, run: _Run, panels: _Panels, day: date) -> list[str]:
    """所有生效订阅两本账的持仓，加上各订阅用当晚快照初筛出的买入候选（不落库）。"""
    snapshot = panels.panel.snapshot(day)
    wanted: set[str] = set()
    errors: list[str] = []
    with _session(ctx) as s:
        for sub in _due_subscriptions(s, day):
            try:
                held = _holdings(s, sub, ctx.cfg, panels, day)
                merged = subscription_config(ctx.cfg, sub)
                strategy = HighDividendLow(merged.strategy, merged.universe)
                wanted |= held
                wanted |= {c.symbol for c in strategy.buy_candidates(day, snapshot, held)}
            except Exception as exc:  # 该使用者在推进时会再出错并单独告警
                errors.append(f"订阅 {sub.id}：{describe(exc)}")
        s.rollback()
    if errors:
        run.detail["recheck_errors"] = errors
    return sorted(wanted)


def _recheck_fundamentals(ctx: RunContext, run: _Run, panels: _Panels, day: date) -> None:
    """重新下载持仓与候选的分红和财务，再重建面板；个别失败记入 detail 并告警，不中止。"""
    symbols = _recheck_symbols(ctx, run, panels, day)
    run.detail["rechecked"] = symbols
    if not symbols:
        return
    failed = refresh_fundamentals(ctx.source, ctx.store, ctx.cfg, symbols)
    panels.rebuild()
    if failed:
        run.detail["fundamentals_failed"] = failed
        lines = [f"{symbol}：{message}" for symbol, message in sorted(failed.items())]
        alert(ctx, day, "基本面复核部分失败", ["以下股票沿用本地旧数据：", *lines])


# ---------- 使用者 ----------


def _days_to_advance(
    s: Session, sub: Subscription, calendar: TradingCalendar, day: date
) -> list[date]:
    """两本账从各自下一个未处理的交易日（从没推进过则为起始日起第一个交易日）到 day。"""
    starts: list[date] = []
    for kind in ("system", "manual"):
        book = repo.get_book_of_kind(s, sub.user_id, sub.id, kind)
        processed = book.processed_through
        starts.append(sub.start_day if processed is None else processed + timedelta(days=1))
    return calendar.between(min(starts), day)


def _advance_user(
    ctx: RunContext,
    s: Session,
    sub: Subscription,
    panels: _Panels,
    day: date,
    notify: bool,
    names: dict[str, str],
) -> None:
    """在调用方的事务里把订阅的两本账逐日推进到 day，建跟踪记录，并为 day 写邮件。"""
    calendar = panels.calendar
    lookback = ctx.cfg.service.backdate_limit_days + _LOOKBACK_SLACK_DAYS
    books = [repo.get_book_of_kind(s, sub.user_id, sub.id, k) for k in ("system", "manual")]
    for d in _days_to_advance(s, sub, calendar, day):
        if d != day:
            panels.require_complete(d)
        panel, market = panels.for_day(d)
        tonight = notify and d == day  # 补推进的更早日子不通知
        advance_system_book(s, sub, ctx.cfg, panel, calendar, d)
        result = advance_manual_book(s, sub, ctx.cfg, panel, market, calendar, d, tonight)
        for book in books:
            open_followups(s, book, d, market, calendar, lookback)
        if tonight:
            queue_signal_mails(s, sub, d, result, ctx.cfg, names)


def _process_subscription(
    ctx: RunContext,
    panels: _Panels,
    sub_id: int,
    day: date,
    notify: bool,
    names: dict[str, str],
) -> str | None:
    """一个订阅一个事务；出任何异常都整体回滚，返回错误描述。"""
    with _session(ctx) as s:
        try:
            sub = s.get(Subscription, sub_id)
            if sub is None or sub.status != "active" or sub.start_day > day:
                return None  # 处理过程中被暂停或关闭
            _advance_user(ctx, s, sub, panels, day, notify, names)
            s.commit()
        except Exception as exc:
            s.rollback()
            _log.exception("订阅 %s 处理出错，已回滚", sub_id)
            return describe(exc)
    return None


def _users_stage(
    ctx: RunContext, panels: _Panels, day: date, notify: bool, names: dict[str, str]
) -> list[dict[str, Any]]:
    with _session(ctx) as s:
        subs = [(sub.id, sub.user_id) for sub in _due_subscriptions(s, day)]
    errors: list[dict[str, Any]] = []
    for sub_id, user_id in subs:
        error = _process_subscription(ctx, panels, sub_id, day, notify, names)
        if error is None:
            continue
        errors.append({"user_id": user_id, "subscription_id": sub_id, "error": error})
        alert(
            ctx,
            day,
            f"使用者 {user_id} 的每日处理出错",
            [f"订阅 {sub_id} 当晚的处理已整体回滚，其他使用者不受影响。", error],
        )
    return errors


def _mail_stage(
    ctx: RunContext, panels: _Panels, day: date, notify: bool, names: dict[str, str]
) -> list[dict[str, Any]]:
    """周报（通知日且是本周最后一个交易日）；返回出错的使用者。"""
    errors: list[dict[str, Any]] = []
    if not (notify and is_last_trading_day_of_week(panels.calendar, day)):
        return errors
    with _session(ctx) as s:
        subs = [(sub.id, sub.user_id) for sub in _due_subscriptions(s, day)]
    for sub_id, user_id in subs:
        with _session(ctx) as s:
            try:
                sub = s.get(Subscription, sub_id)
                if sub is not None:
                    queue_weekly(
                        s, sub, day, panels.calendar, ctx.store, panels.market, ctx.cfg, names
                    )
                s.commit()
            except Exception as exc:
                s.rollback()
                _log.exception("订阅 %s 的周报出错", sub_id)
                errors.append(
                    {"user_id": user_id, "subscription_id": sub_id, "error": describe(exc)}
                )
                alert(ctx, day, f"使用者 {user_id} 的周报出错", [describe(exc)])
    return errors


# ---------- 一天 ----------


def run_day(ctx: RunContext, day: date, notify: bool, update_data: bool) -> str:
    """完整跑一个交易日，返回 daily_runs.status。

    notify 为假（补跑的更早日子）时新信号记为未通知、不写使用者邮件；update_data 为真时先刷新
    交易日历并增量更新到 day。day 不是交易日时抛 ValueError。
    """
    stored = _stored_calendar(ctx.store)
    if stored is not None and not stored.is_trading_day(day):
        raise ValueError(f"{day} 不是交易日")
    run = _Run(ctx, day)
    _log.info("开始每日运行 %s（通知：%s，更新数据：%s）", day, notify, update_data)

    if update_data:
        try:
            _refresh_trade_days(ctx)
            problems = _update_data(ctx, day)
        except Exception as exc:
            _log.exception("数据更新失败")
            error = describe(exc)
            return _data_failed(ctx, run, "data", "数据更新失败", {"error": error}, [error])
        if problems:
            return _data_failed(
                ctx, run, "data", "数据更新发现问题", {"problems": problems}, problems
            )

    run.stage("check")
    calendar = stored or TradingCalendar([])
    try:
        calendar = load_calendar(ctx.store)
        if calendar.is_trading_day(day):
            problems = check_integrity(ctx.store, calendar, day)
        else:
            problems = [f"{day} 不是交易日"]
    except Exception as exc:
        problems = [f"无法检查数据：{describe(exc)}"]
    if problems:
        return _data_failed(
            ctx, run, "check", "数据完整性检查未通过", {"problems": problems}, problems
        )

    run.stage("panel")
    try:
        panels = _Panels(ctx, calendar, day)
        _recheck_fundamentals(ctx, run, panels, day)
        names = security_names(ctx.store)
    except Exception as exc:
        _log.exception("构建指标面板失败")
        error = describe(exc)
        return _data_failed(ctx, run, "panel", "指标面板构建失败", {"error": error}, [error])

    run.stage("users")
    user_errors = _users_stage(ctx, panels, day, notify, names)
    run.detail["user_errors"] = user_errors
    other_errors = False
    try:
        with _session(ctx) as s:
            filled = fill_followups(s, calendar, panels.market, day, ctx.cfg.service.followup_days)
            s.commit()
        run.detail["followups_filled"] = filled
    except Exception as exc:
        _log.exception("填写跟踪记录出错")
        other_errors = True
        run.detail["followups_error"] = describe(exc)
        alert(ctx, day, "跟踪记录填写出错", [describe(exc)])

    run.stage("mail")
    weekly_errors = _mail_stage(ctx, panels, day, notify, names)
    if weekly_errors:
        run.detail["weekly_errors"] = weekly_errors
    _send_quietly(ctx, day, run)

    failed = bool(user_errors or weekly_errors or other_errors)
    status = run.finish(DONE_WITH_ERRORS if failed else DONE, "done")
    _log.info("每日运行 %s 结束：%s", day, status)
    return status


# ---------- 补跑 ----------


def pending_days(
    s: Session, calendar: TradingCalendar, as_of: date, first_day: date | None
) -> list[date]:
    """first_day 到 as_of 之间、daily_runs 里还不是 done/done_with_errors 的交易日（升序）。"""
    if first_day is None:
        return []
    stmt = select(DailyRun.trade_day).where(
        DailyRun.status.in_(FINISHED), DailyRun.trade_day.between(first_day, as_of)
    )
    finished = set(s.scalars(stmt))
    return [d for d in calendar.between(first_day, as_of) if d not in finished]


def first_start_day(s: Session) -> date | None:
    """生效订阅中最早的起始日；没有生效订阅时为 None。"""
    starts = [sub.start_day for sub in admin_repo.list_active_subscriptions(s)]
    return min(starts) if starts else None


def due_day(now: datetime, calendar: TradingCalendar, run_time: str) -> date:
    """补跑的截止日：最近一个已收盘的交易日，但当天要过了每日运行时刻才算。"""
    as_of = latest_closed_day(now, calendar)
    local = now.astimezone(MARKET_TZ)
    if as_of == local.date() and local.time() < parse_clock(run_time):
        return calendar.offset(as_of, -1)
    return as_of


def catch_up(ctx: RunContext) -> list[tuple[date, str]]:
    """按日期顺序补上所有没跑完的交易日，返回 [(交易日, 状态)]。

    先刷新交易日历并做一次数据更新（到 as_of），之后逐日 run_day，只有 as_of 那天通知；
    某天 data_failed 即停止，更晚的日子留到下次（系统账必须按顺序推进）。
    """
    with _session(ctx) as s:
        first = first_start_day(s)
    if first is None:
        return []
    refresh_error: str | None = None
    try:
        _refresh_trade_days(ctx)
    except SharedTableError as exc:
        refresh_error = str(exc)  # 先用已存储的日历判断有没有要补的日子
    calendar = load_calendar(ctx.store)
    as_of = due_day(ctx.now(), calendar, ctx.cfg.service.run_time)
    with _session(ctx) as s:
        days = pending_days(s, calendar, as_of, first)
    if not days:
        return []
    _log.info("待补跑 %d 个交易日：%s 至 %s", len(days), days[0], days[-1])
    try:
        if refresh_error is not None:
            raise SharedTableError(refresh_error)
        problems = _update_data(ctx, as_of)
    except Exception as exc:
        _log.exception("补跑前的数据更新失败")
        error = describe(exc)
        run = _Run(ctx, days[0])
        status = _data_failed(ctx, run, "data", "数据更新失败", {"error": error}, [error])
        return [(days[0], status)]
    if problems:
        run = _Run(ctx, days[0])
        status = _data_failed(
            ctx, run, "data", "数据更新发现问题", {"problems": problems}, problems
        )
        return [(days[0], status)]
    results: list[tuple[date, str]] = []
    for day in days:
        status = run_day(ctx, day, notify=day == as_of, update_data=False)
        results.append((day, status))
        if status == DATA_FAILED:
            break
    return results
