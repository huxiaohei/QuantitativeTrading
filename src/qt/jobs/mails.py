"""每日任务的邮件组装：把当晚两本账的结果变成发件箱里的邮件。

只写入发件箱（enqueue），不发送、不提交；事务由每日任务决定。去重键：
buy/sell/sell-remind/abandon 为 `{种类}:{手动账编号}:{交易日}`，
风控为 `risk:{手动账编号}:{交易日}:{类型}`，周报为 `weekly:{使用者编号}:{周末交易日}`，
管理员告警为 `admin:{日期}:{标题}`。
"""

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import Book, BookDay, LedgerEntry, Signal, Subscription
from qt.db.models import RiskEvent as RiskEventRow
from qt.mail.outbox import enqueue
from qt.mail.render import (
    BooksSummary,
    HoldingView,
    Mail,
    RiskEventView,
    SignalView,
    WeeklyView,
    render_abandon_reminder,
    render_admin_alert,
    render_buy_signals,
    render_risk_alert,
    render_sell_signals,
    render_weekly,
)
from qt.service.accounts import subscription_config
from qt.service.manual_book import ManualState
from qt.service.manual_ops import ManualDayResult, manual_state
from qt.service.market import StoreMarket

# 信号指标的中文名；比例类指标按百分数显示
_PERCENT_METRICS: Mapping[str, str] = {
    "dividend_yield": "股息率(%)",
    "price_position": "价格位置(%)",
    "roe_avg3": "三年平均ROE(%)",
    "payout_ratio": "分红率(%)",
    "profit_yoy": "净利润同比(%)",
    "debt_ratio": "资产负债率(%)",
}
_PLAIN_METRICS: Mapping[str, str] = {"dividend_years": "连续分红年数"}
# 风控事件：数据库里的类型 -> 邮件模板的类型
_RISK_KINDS: Mapping[str, str] = {
    "warn": "drawdown_warning",
    "pause": "buy_paused",
    "resume": "buy_resumed",
}
_STATUS_LABELS: Mapping[str, str] = {
    "created": "已生成",
    "notified": "已通知",
    "executed": "已执行",
    "abandoned": "已放弃",
    "voided": "已作废",
    "unnotified": "未通知",
}
_SIDE_LABELS: Mapping[str, str] = {"buy": "买入", "sell": "卖出", "transfer_in": "转入"}
_BOOK_LABELS: Mapping[str, str] = {"manual": "手动账", "system": "系统账"}
_OPEN = ("created", "notified")


def security_names(store: ParquetStore) -> dict[str, str]:
    """股票代码 -> 名称（证券表里没有的不在其中）。"""
    secs = store.read(Table.SECURITIES)
    return {str(s): str(n) for s, n in zip(secs["symbol"], secs["name"], strict=True)}


def alert_admin(s: Session, admin_email: str, day: date, title: str, lines: Sequence[str]) -> bool:
    """给管理员的系统异常邮件入队；同一天同一标题只入队一次。"""
    mail = render_admin_alert(day, title, lines)
    return _put(s, None, admin_email, "admin", f"admin:{day}:{title}", mail)


def _put(s: Session, user_id: int | None, to_addr: str, kind: str, key: str, mail: Mail) -> bool:
    return enqueue(s, user_id, to_addr, kind, key, mail.subject, mail.html, mail.text)


def _metrics(raw: Mapping[str, object]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue  # 缺失（null）的指标不显示
        number = float(value)
        if not math.isfinite(number):
            continue
        if key in _PERCENT_METRICS:
            out[_PERCENT_METRICS[key]] = number * 100
        else:
            out[_PLAIN_METRICS.get(key, key)] = number
    return out


def signal_view(sig: Signal, names: Mapping[str, str]) -> SignalView:
    """数据库里的信号 -> 邮件展示字段。"""
    return SignalView(
        side=sig.side,
        symbol=sig.symbol,
        name=names.get(sig.symbol, ""),
        shares=round(sig.shares),
        ref_price=sig.ref_price,
        reason=sig.reason,
        exit_reason=sig.exit_reason,
        metrics=_metrics(sig.metrics),
        stop_price=sig.stop_price,
        take_price=sig.take_price,
        valid_until=sig.valid_until,
    )


def _book(s: Session, sub: Subscription, kind: str) -> Book:
    return repo.get_book_of_kind(s, sub.user_id, sub.id, kind)


def _book_day(s: Session, book: Book, day: date) -> BookDay | None:
    return s.get(BookDay, (book.id, day))


def books_summary(s: Session, sub: Subscription, day: date, manual: ManualState) -> BooksSummary:
    """两本账截至 day 的概况；手动账取重放结果，系统账取当日净值与保存的状态。"""
    nav = manual.unit_nav[-1] if manual.unit_nav else 1.0
    peak = manual.risk.peak
    drawdown = max(0.0, 1 - nav / peak) if peak else 0.0
    system = _book(s, sub, "system")
    row = repo.get_system_state(s, sub.user_id, system.id)
    engine = load_state(row.state)[0] if row is not None else None
    cash = engine.cash if engine is not None else sub.initial_cash
    today = _book_day(s, system, day)
    return BooksSummary(
        manual_equity=manual.equity[-1] if manual.equity else sub.initial_cash,
        manual_cash=manual.cash,
        manual_positions=len(manual.positions),
        manual_drawdown=drawdown,
        system_equity=today.equity if today is not None else cash,
        system_cash=cash,
        system_positions=len(engine.positions) if engine is not None else 0,
    )


def queue_signal_mails(
    s: Session,
    sub: Subscription,
    day: date,
    result: ManualDayResult,
    cfg: AppConfig,
    names: Mapping[str, str],
) -> int:
    """按手动账当晚的结果写买入、卖出、再提醒、风控、放弃提醒邮件，返回新入队的封数。

    买卖信号邮件只在手动账当晚有该类信号时才写，系统账同日的信号附在后面作对照。
    """
    user = repo.get_user(s, sub.user_id)
    manual = _book(s, sub, "manual")
    system = _book(s, sub, "system")
    system_today = [
        sig for sig in repo.list_signals(s, sub.user_id, system.id) if sig.signal_day == day
    ]
    books = books_summary(s, sub, day, result.state)
    to = user.email
    queued = 0

    def views(signals: Sequence[Signal]) -> list[SignalView]:
        return [signal_view(sig, names) for sig in signals]

    def put(kind: str, key: str, mail: Mail) -> None:
        nonlocal queued
        queued += _put(s, user.id, to, kind, key, mail)

    if result.new_buy_signals:
        others = [sig for sig in system_today if sig.side == "buy"]
        mail = render_buy_signals(day, views(result.new_buy_signals), views(others), books, to)
        put("buy", f"buy:{manual.id}:{day}", mail)
    if result.new_sell_signals:
        others = [sig for sig in system_today if sig.side == "sell"]
        mail = render_sell_signals(
            day, views(result.new_sell_signals), views(others), books, False, to
        )
        put("sell", f"sell:{manual.id}:{day}", mail)
    if result.reminders:
        mail = render_sell_signals(day, views(result.reminders), [], books, True, to)
        put("sell-remind", f"sell-remind:{manual.id}:{day}", mail)
    risk_cfg = subscription_config(cfg, sub).risk
    thresholds = {"warn": risk_cfg.warn_drawdown, "pause": risk_cfg.pause_drawdown}
    for event in result.risk_events:
        put(
            "risk",
            f"risk:{manual.id}:{day}:{event.kind}",
            _risk_mail(day, event, thresholds, books),
        )
    if result.abandoned:
        mail = render_abandon_reminder(day, views(result.abandoned))
        put("abandon", f"abandon:{manual.id}:{day}", mail)
    return queued


def _risk_mail(
    day: date, event: RiskEventRow, thresholds: Mapping[str, float], books: BooksSummary
) -> Mail:
    view = RiskEventView(
        kind=_RISK_KINDS[event.kind],
        drawdown=abs(event.drawdown),
        threshold=thresholds.get(event.kind),
        message=event.message,
    )
    return render_risk_alert(day, view, books)


# ---------- 周报 ----------


def is_last_trading_day_of_week(calendar: TradingCalendar, day: date) -> bool:
    """day 是不是本周最后一个交易日（下一个交易日落在下一周）。日历排不到下一天时按周五判断。"""
    try:
        following = calendar.offset(day, 1)
    except ValueError:
        return day.weekday() == 4
    return following.isocalendar()[:2] != day.isocalendar()[:2]


def _previous_trading_day(calendar: TradingCalendar, day: date) -> date | None:
    try:
        return calendar.offset(day, -1)
    except ValueError:
        return None


def _week_return(s: Session, book: Book, base: date | None, day: date) -> float:
    """份额净值的本周涨跌；本周才开始的账本以 1.0 为起点。"""
    end = _book_day(s, book, day)
    if end is None:
        return 0.0
    start = _book_day(s, book, base) if base is not None else None
    return end.unit_nav / (start.unit_nav if start is not None else 1.0) - 1


def _benchmark_return(
    store: ParquetStore, cfg: AppConfig, base: date | None, day: date
) -> float | None:
    """配置里第一个在上周末与本周末都有收盘价的基准的本周涨跌；都没有则为 None。"""
    if base is None:
        return None
    frame = store.read(Table.BENCHMARKS, start=base, end=day)
    for code in cfg.backtest.benchmarks:
        rows = frame[frame["code"] == code].sort_values("day")
        start = rows[rows["day"] == pd.Timestamp(base)]
        if start.empty or len(rows) < 2:
            continue
        first = float(start["close"].iloc[0])
        last = float(rows["close"].iloc[-1])
        if first > 0:
            return last / first - 1
    return None


def _signal_lines(
    s: Session,
    sub: Subscription,
    books: Sequence[Book],
    week: tuple[date, date],
    names: Mapping[str, str],
) -> list[str]:
    lines: list[str] = []
    for book in books:
        for sig in repo.list_signals(s, sub.user_id, book.id):
            if not week[0] <= sig.signal_day <= week[1]:
                continue
            lines.append(
                f"{sig.signal_day} {_BOOK_LABELS[book.kind]} {_SIDE_LABELS[sig.side]}信号 "
                f"{sig.symbol} {names.get(sig.symbol, '')} {round(sig.shares)} 股"
                f"（{_STATUS_LABELS.get(sig.status, sig.status)}）"
            )
    manual = next(b for b in books if b.kind == "manual")
    for sig in repo.list_signals(s, sub.user_id, manual.id, "sell", _OPEN):
        lines.append(
            f"未处理的卖出信号：{sig.signal_day} {sig.symbol} {names.get(sig.symbol, '')} "
            f"{round(sig.shares)} 股，卖出后请录入"
        )
    return lines


def _trade_lines(
    s: Session,
    sub: Subscription,
    books: Sequence[Book],
    week: tuple[date, date],
    names: Mapping[str, str],
) -> list[str]:
    lines: list[str] = []
    for book in books:
        entries = repo.list_ledger_entries(s, sub.user_id, book.id)
        voided = {e.voids_entry_id for e in entries if e.kind == "void"}
        for e in entries:
            if e.id in voided or e.kind not in _SIDE_LABELS or e.symbol is None:
                continue
            if week[0] <= e.trade_day <= week[1]:
                lines.append(_entry_line(book, e, names))
    return lines


def _entry_line(book: Book, e: LedgerEntry, names: Mapping[str, str]) -> str:
    symbol = e.symbol or ""
    return (
        f"{e.trade_day} {_BOOK_LABELS[book.kind]} {_SIDE_LABELS[e.kind]} {symbol} "
        f"{names.get(symbol, '')} {round(e.shares)} 股，价格 {e.price:.2f}"
    )


def queue_weekly(
    s: Session,
    sub: Subscription,
    day: date,
    calendar: TradingCalendar,
    store: ParquetStore,
    market: StoreMarket,
    cfg: AppConfig,
    names: Mapping[str, str],
) -> bool:
    """为开启周报、两本账都已推进到 day 的使用者写本周周报；返回是否新入队。"""
    user = repo.get_user(s, sub.user_id)
    if not user.weekly_mail or user.status != "active":
        return False
    manual = _book(s, sub, "manual")
    system = _book(s, sub, "system")
    if any(b.processed_through is None or b.processed_through < day for b in (manual, system)):
        return False  # 当晚这位使用者出错了，没有完整数据
    week_days = calendar.between(day - timedelta(days=day.weekday()), day)
    week = (week_days[0], day)
    base = _previous_trading_day(calendar, week[0])
    state = manual_state(s, sub.user_id, sub.id, market, calendar, cfg, day)
    holdings = [
        HoldingView(
            symbol=pos.symbol,
            name=names.get(pos.symbol, ""),
            shares=round(pos.shares),
            cost=pos.cost / pos.shares,
            last_price=(last := market.last_close(day, pos.symbol)),
            pnl_pct=(pos.shares * last + pos.cash_received) / pos.cost - 1,
        )
        for pos in state.positions.values()
    ]
    manual_today = _book_day(s, manual, day)
    system_today = _book_day(s, system, day)
    books = (manual, system)
    report = WeeklyView(
        week_start=week[0],
        manual_equity=manual_today.equity if manual_today is not None else 0.0,
        system_equity=system_today.equity if system_today is not None else 0.0,
        manual_week_return=_week_return(s, manual, base, day),
        system_week_return=_week_return(s, system, base, day),
        benchmark_week_return=_benchmark_return(store, cfg, base, day),
        holdings=holdings,
        signal_lines=_signal_lines(s, sub, books, week, names),
        trade_lines=_trade_lines(s, sub, books, week, names),
    )
    return _put(
        s, user.id, user.email, "weekly", f"weekly:{user.id}:{day}", render_weekly(day, report)
    )
