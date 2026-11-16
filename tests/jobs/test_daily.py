"""每日任务：数据、检查、面板、逐个使用者推进两本账与写邮件、发送；重跑、补跑与出错隔离。"""

from collections import Counter
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.backtest.report import run_period
from qt.backtest.state import load_state
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import Base, BookDay, Outbox, Signal, Subscription, Trade, User
from qt.features import FeaturePanel, build_panel
from qt.jobs import daily
from qt.jobs.daily import catch_up, pending_days, run_day
from qt.mail.outbox import MAX_PENDING_AGE
from qt.mail.render import HoldingView, WeeklyView, render_weekly
from qt.mail.send import Sender
from qt.models import Side
from qt.service.accounts import close_subscription, pause_subscription, subscription_config
from qt.service.manual_book import Market
from qt.service.manual_ops import ManualDayResult, record_holding, record_trade
from qt.service.market import StoreMarket
from tests.fakes import (
    TRADING_SYMBOLS,
    FakeSource,
    make_industries,
    make_securities,
    snapshot_from_bars,
)
from tests.jobs.conftest import (
    ADMIN,
    CFG,
    DAY,
    BrokenSource,
    Env,
    book_signals,
    daily_run,
    evening,
    mirror_source,
    processed_through,
    subscribe,
)


class NoCalendarSource(FakeSource):
    """交易日历取不到。"""

    def trade_days(self) -> pd.DataFrame:
        raise ConnectionError("模拟交易日历不可用")


class _Killed(BaseException):
    """模拟进程被杀：不是 Exception，任何 except Exception 都拦不住。"""


def _table_counts(db: Session) -> dict[str, int]:
    db.rollback()
    return {
        t.name: int(db.scalar(select(func.count()).select_from(t)) or 0)
        for t in Base.metadata.sorted_tables
    }


def _drop_bars(store: ParquetStore, symbols: list[str], day: date) -> dict[str, pd.DataFrame]:
    """删掉这些股票在 day 的日线，返回原表以便之后补回。"""
    originals: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        bars = store.read(Table.BARS, symbols=[symbol])
        originals[symbol] = bars
        kept = bars[bars["day"] != pd.Timestamp(day)].reset_index(drop=True)
        store.write(Table.BARS, kept, symbol=symbol)
    return originals


def _equity(db: Session, sub: Subscription, kind: str) -> list[float]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, kind)
    rows = db.scalars(select(BookDay).where(BookDay.book_id == book.id).order_by(BookDay.day))
    return [r.equity for r in rows]


def test_run_day_advances_both_books_and_sends_one_buy_mail(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub = subscribe(db, "a@example.com", day)
    assert run_day(env.ctx, day, notify=True, update_data=False) == "done"
    assert processed_through(db, sub, "system") == processed_through(db, sub, "manual") == day
    assert [m.subject for m in env.sender.sent] == ["【买入信号】2022-01-03 4 只"]
    mail = env.sender.sent[0]
    assert mail.to_addr == "a@example.com"
    assert (
        "手动账（你的实际账户，信号以此为准）" in mail.text
        and "系统账（自动执行，仅供对照）" in mail.text
    )
    assert "股息率" in mail.text and "建议买入" not in mail.text and "推荐" not in mail.text
    assert {s.status for s in book_signals(db, sub, "manual")} == {"notified"}
    run = daily_run(db, day)
    assert run is not None and (run.status, run.stage) == ("done", "done")
    assert run.finished_at is not None and run.finished_at.tzinfo is not None


def test_rerun_same_day_creates_no_duplicates(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    run_day(env.ctx, day, True, False)
    before = _table_counts(db)
    n = len(env.sender.sent)
    assert run_day(env.ctx, day, True, False) == "done"
    assert _table_counts(db) == before and len(env.sender.sent) == n


def test_integrity_failure_emits_no_signals_and_alerts_admin(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub = subscribe(db, "a@example.com", day)
    _drop_bars(env.store, ["600001", "600002"], day)  # 当日日线只有 50% 的股票
    assert run_day(env.ctx, day, True, False) == "data_failed"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Signal)) == 0
    assert processed_through(db, sub, "system") is None
    assert [m.to_addr for m in env.sender.sent] == [ADMIN]
    assert env.sender.sent[0].subject.startswith("【系统异常】")
    run = daily_run(db, day)
    assert run is not None and "previous" not in run.detail
    assert run is not None and (run.status, run.stage) == ("data_failed", "check")
    assert "2/4" in str(run.detail["problems"])
    # 重跑仍失败，同一天同一标题的告警只发一次
    assert run_day(env.ctx, day, True, False) == "data_failed"
    assert len(env.sender.sent) == 1
    run = daily_run(db, day)
    assert run is not None and "2/4" in str(run.detail["problems"])
    [previous] = run.detail["previous"]  # 上一次的失败记录保留
    assert previous["status"] == "data_failed" and "2/4" in str(previous["problems"])


def test_data_update_failure_marks_run_failed_and_alerts(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub = subscribe(db, "a@example.com", day)
    env.source = mirror_source(env.store, BrokenSource)
    assert run_day(env.ctx, day, True, True) == "data_failed"
    assert processed_through(db, sub, "manual") is None
    assert [m.subject for m in env.sender.sent] == ["【系统异常】数据更新失败"]
    run = daily_run(db, day)
    assert run is not None and run.stage == "data"
    assert "模拟数据源不可用" in run.detail["error"]


def test_catch_up_processes_missing_days_in_order_and_notifies_only_latest(
    env: Env, db: Session
) -> None:
    days = env.days
    sub = subscribe(db, "a@example.com", days[240])
    env.clock.now = evening(days[248])
    first = catch_up(env.ctx)
    assert first == [(d, "done") for d in days[240:249]]
    # 已过完成期限才补跑的日子：管理员收到一次说明
    [late] = [m for m in env.sender.sent if m.to_addr == ADMIN]
    assert late.subject == "【系统异常】补跑了已超时的交易日"
    listed = late.text.strip().splitlines()[-1]
    assert listed == "、".join(str(d) for d in days[240:248])  # 不含当晚（已通知的）那天
    env.sender.sent.clear()
    # 上次成功是 5 个交易日前
    env.clock.now = evening(days[253])
    five_days = days[249:254]
    result = catch_up(env.ctx)
    assert [d for d, _ in result] == five_days and all(st == "done" for _, st in result)
    signals = [s for s in book_signals(db, sub, "manual") if s.signal_day in five_days]
    earlier = [s.status for s in signals if s.signal_day < five_days[-1]]
    latest = [s.status for s in signals if s.signal_day == five_days[-1]]
    assert earlier and all(v == "unnotified" for v in earlier)
    assert latest and all(v == "notified" for v in latest)
    user_mails = [m.subject for m in env.sender.sent if m.to_addr != ADMIN]
    assert user_mails == [f"【买入信号】{five_days[-1]} {len(latest)} 只"]
    # 系统账与一次性回测一致（逐日面板、每晚基本面复核都不改变结果）
    merged = subscription_config(CFG, sub)
    whole = run_period(merged, env.store, days[240], days[253])
    assert _equity(db, sub, "system") == pytest.approx(list(whole.equity), rel=0, abs=0)
    # 再补跑一段（含止损卖出），仍逐项相同
    env.clock.now = evening(days[300])
    assert all(st == "done" for _, st in catch_up(env.ctx))
    whole = run_period(merged, env.store, days[240], days[300])
    equity = _equity(db, sub, "system")
    assert len(equity) == 61
    assert equity == pytest.approx(list(whole.equity), rel=0, abs=0)
    assert [x.hex() for x in equity] == [float(x).hex() for x in whole.equity]
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "system")
    trades = db.scalars(select(Trade).where(Trade.book_id == book.id))
    assert sorted((t.symbol, t.exit_day, t.proceeds) for t in trades) == sorted(
        (t.symbol, t.exit_day, t.proceeds) for t in whole.trades
    )
    assert whole.trades  # 区间内确有卖出


def test_catch_up_stops_at_first_data_failure_then_resumes_next_time(env: Env, db: Session) -> None:
    days = env.days
    sub = subscribe(db, "a@example.com", days[DAY])
    originals = _drop_bars(env.store, ["600001", "600002"], days[DAY + 1])
    env.clock.now = evening(days[DAY + 2])
    assert catch_up(env.ctx) == [(days[DAY], "done"), (days[DAY + 1], "data_failed")]
    assert daily_run(db, days[DAY + 2]) is None
    assert processed_through(db, sub, "system") == days[DAY]
    for symbol, frame in originals.items():
        env.store.write(Table.BARS, frame, symbol=symbol)
    assert catch_up(env.ctx) == [(days[DAY + 1], "done"), (days[DAY + 2], "done")]
    assert processed_through(db, sub, "manual") == days[DAY + 2]


def test_catch_up_without_subscriptions_does_nothing(env: Env, db: Session) -> None:
    assert catch_up(env.ctx) == []
    assert env.sender.sent == []
    db.rollback()
    assert pending_days(db, env.calendar, env.days[DAY], None) == []
    assert (
        pending_days(db, env.calendar, env.days[DAY], env.days[DAY - 2])
        == env.days[DAY - 2 : DAY + 1]
    )


def test_one_user_error_does_not_block_others(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = env.days[DAY]
    user_a = subscribe(db, "a@example.com", day)
    user_b = subscribe(db, "b@example.com", day)
    real = daily.advance_manual_book

    def broken(
        s: Session,
        sub: Subscription,
        cfg: AppConfig,
        panel: FeaturePanel,
        market: Market,
        calendar: TradingCalendar,
        d: date,
        notify: bool,
    ) -> ManualDayResult:
        if sub.id == user_a.id:
            raise RuntimeError("模拟手动账出错")
        return real(s, sub, cfg, panel, market, calendar, d, notify)

    monkeypatch.setattr(daily, "advance_manual_book", broken)
    assert run_day(env.ctx, day, True, False) == "done_with_errors"
    assert processed_through(db, user_a, "system") is None  # 甲整体回滚
    assert processed_through(db, user_b, "manual") == day
    [admin] = [m for m in env.sender.sent if m.to_addr == ADMIN]
    assert admin.subject == f"【系统异常】使用者 {user_a.user_id} 订阅 {user_a.id} 的每日处理出错"
    assert [m.to_addr for m in env.sender.sent if m.to_addr != ADMIN] == ["b@example.com"]
    run = daily_run(db, day)
    assert run is not None
    errors = run.detail["user_errors"]
    assert [e["user_id"] for e in errors] == [user_a.user_id]
    assert "模拟手动账出错" in errors[0]["error"]
    # 修复后重跑：甲被补上，乙不重复
    monkeypatch.undo()
    b_signals = len(book_signals(db, user_b, "manual"))
    env.sender.sent.clear()
    assert run_day(env.ctx, day, True, False) == "done"
    assert processed_through(db, user_a, "manual") == day
    assert len(book_signals(db, user_b, "manual")) == b_signals
    assert [m.to_addr for m in env.sender.sent] == ["a@example.com"]


def test_killed_between_users_and_mail_then_rerun_sends_each_mail_once(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    subscribe(db, "b@example.com", day)

    def killed(
        s: Session,
        sender: Sender,
        now: datetime | None = None,
        max_age: timedelta = MAX_PENDING_AGE,
    ) -> tuple[int, int]:
        raise _Killed

    monkeypatch.setattr(daily, "send_pending", killed)
    with pytest.raises(_Killed):
        run_day(env.ctx, day, True, False)
    assert env.sender.sent == []
    run = daily_run(db, day)
    assert run is not None and (run.status, run.stage) == ("running", "mail")
    monkeypatch.undo()
    assert run_day(env.ctx, day, True, False) == "done"
    counts = Counter((m.to_addr, m.subject) for m in env.sender.sent)
    assert sorted(to for to, _ in counts) == ["a@example.com", "b@example.com"]
    assert set(counts.values()) == {1}
    assert run_day(env.ctx, day, True, False) == "done"
    assert len(env.sender.sent) == 2


def test_smtp_failure_keeps_run_done_and_mail_pending(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    env.sender.failing = True
    assert run_day(env.ctx, day, True, False) == "done"
    db.rollback()
    [row] = db.scalars(select(Outbox))
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.last_error is not None and "模拟 SMTP 不可用" in row.last_error
    env.sender.failing = False
    assert run_day(env.ctx, day, True, False) == "done"
    assert [m.subject for m in env.sender.sent] == ["【买入信号】2022-01-03 4 只"]


def test_dead_mail_alerts_admin_once_per_day(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    env.sender.fail_to = {"a@example.com"}
    for _ in range(6):  # 48 小时内反复失败只记次数，不放弃
        assert run_day(env.ctx, day, True, False) == "done"
    db.rollback()
    [row] = db.scalars(select(Outbox).where(Outbox.to_addr == "a@example.com"))
    assert (row.status, row.attempts) == ("pending", 6)
    assert not [m for m in env.sender.sent if m.to_addr == ADMIN]
    row.created_at = env.clock.now - timedelta(hours=48, minutes=1)  # 入队已满 48 小时
    db.commit()
    assert run_day(env.ctx, day, True, False) == "done"
    db.rollback()
    [dead] = db.scalars(select(Outbox).where(Outbox.to_addr == "a@example.com"))
    assert dead.status == "failed"
    alerts = [m for m in env.sender.sent if m.to_addr == ADMIN]
    assert [m.subject for m in alerts] == ["【系统异常】邮件发送失败"]
    assert "a@example.com" in alerts[0].text and "outbox retry" in alerts[0].text
    assert run_day(env.ctx, day, True, False) == "done"
    assert len([m for m in env.sender.sent if m.to_addr == ADMIN]) == 1


def _add_bystander(store: ParquetStore) -> None:
    """加一只不分红的股票 600005：有日线，但永远不是买入候选。"""
    bars = store.read(Table.BARS, symbols=["600001"])
    bars["symbol"] = "600005"
    store.write(Table.BARS, bars, symbol="600005")
    symbols = [*TRADING_SYMBOLS, "600005"]
    store.write(
        Table.SECURITIES, make_securities([(s, "sh_main", date(2010, 1, 1), None) for s in symbols])
    )
    store.write(Table.INDUSTRIES, make_industries([(s, "银行", False) for s in symbols]))


def test_fundamentals_rechecked_for_holdings_and_candidates_before_signals(
    env: Env, db: Session
) -> None:
    days = env.days
    day = days[DAY]
    _add_bystander(env.store)
    sub = subscribe(db, "a@example.com", day)
    panel = build_panel(env.store, env.calendar, CFG, days[0], day)
    market = StoreMarket(env.store, panel, CFG)
    record_holding(
        db, sub.user_id, sub.id, market, env.calendar, CFG, day, "600001", days[DAY - 5], 10.0, 1000
    )
    db.commit()
    # 数据源里 600004 的 2020 财年分红已削减（本地文件还是旧的）
    source = mirror_source(env.store)
    dividends = env.store.read(Table.DIVIDENDS)
    cut = (dividends["symbol"] == "600004") & (dividends["fiscal_year"] == 2020)
    dividends.loc[cut, "cash_per_share"] = 0.3
    source = type(source)(
        trade_days=env.store.read(Table.TRADE_DAYS),
        securities=env.store.read(Table.SECURITIES),
        bars=env.store.read(Table.BARS),
        dividends=dividends,
        financials=env.store.read(Table.FINANCIALS),
        industries=env.store.read(Table.INDUSTRIES),
    )
    env.source = source
    assert run_day(env.ctx, day, True, False) == "done"
    called = {args[0] for name, args in source.calls if name == "dividends"}
    assert called == {"600001", "600002", "600003", "600004"}  # 持仓与候选，不含 600005
    buys = [s.symbol for s in book_signals(db, sub, "manual") if s.side == "buy"]
    assert sorted(buys) == ["600002", "600003"]
    assert "600004" not in {s.symbol for s in book_signals(db, sub, "system")}


def test_weekly_mail_only_on_last_trading_day_of_week_and_respects_user_switch(
    env: Env, db: Session
) -> None:
    days = env.days
    subscribe(db, "a@example.com", days[DAY])
    off = subscribe(db, "b@example.com", days[DAY])
    user = db.get(User, off.user_id)
    assert user is not None
    user.weekly_mail = False
    db.commit()
    for i in range(DAY, DAY + 4):  # 周一到周四
        assert run_day(env.ctx, days[i], True, False) == "done"
    assert not [m for m in env.sender.sent if m.subject.startswith("【周报】")]
    friday = days[DAY + 4]
    assert friday.weekday() == 4
    assert run_day(env.ctx, friday, True, False) == "done"
    weekly = [m for m in env.sender.sent if m.subject.startswith("【周报】")]
    assert [(m.to_addr, m.subject) for m in weekly] == [("a@example.com", "【周报】2022-01-07")]
    text = weekly[0].text
    assert "本周（2022-01-03 至 2022-01-07）" in text
    assert "手动账" in text and "系统账" in text and "基准本周" in text
    assert "== 本周信号 ==" in text and "== 本周成交 ==" in text
    # 重跑周五不重发周报
    assert run_day(env.ctx, friday, True, False) == "done"
    assert len([m for m in env.sender.sent if m.subject.startswith("【周报】")]) == 1


def test_paused_and_closed_subscriptions_are_skipped(env: Env, db: Session) -> None:
    day = env.days[DAY]
    active = subscribe(db, "a@example.com", day)
    paused = subscribe(db, "b@example.com", day)
    closed = subscribe(db, "c@example.com", day)
    pause_subscription(db, paused.user_id, paused.id)
    close_subscription(db, closed.user_id, closed.id)
    db.commit()
    assert run_day(env.ctx, day, True, False) == "done"
    assert processed_through(db, active, "manual") == day
    for sub in (paused, closed):
        assert processed_through(db, sub, "system") is None
        assert processed_through(db, sub, "manual") is None
    assert [m.to_addr for m in env.sender.sent] == ["a@example.com"]


def test_subscription_starting_later_is_skipped_and_late_one_is_caught_up(
    env: Env, db: Session
) -> None:
    days = env.days
    later = subscribe(db, "a@example.com", days[DAY + 1])
    backdated = subscribe(db, "b@example.com", days[DAY - 15])  # 起始日在过去、从没跑过
    assert run_day(env.ctx, days[DAY], True, False) == "done"
    assert processed_through(db, later, "system") is None
    assert processed_through(db, backdated, "system") == days[DAY]
    db.rollback()
    book = repo.get_book_of_kind(db, backdated.user_id, backdated.id, "system")
    rows = db.scalars(select(BookDay.day).where(BookDay.book_id == book.id).order_by(BookDay.day))
    assert list(rows) == days[DAY - 15 : DAY + 1]  # 从起始日起逐日推进，没有跳过
    statuses = {(s.signal_day, s.status) for s in book_signals(db, backdated, "manual")}
    assert {st for d, st in statuses if d < days[DAY]} <= {"unnotified"}
    assert {st for d, st in statuses if d == days[DAY]} == {"notified"}
    assert [m.to_addr for m in env.sender.sent] == ["b@example.com"]
    # 补推进用的是更宽的面板：系统账仍与一次性回测逐项相同
    whole = run_period(subscription_config(CFG, backdated), env.store, days[DAY - 15], days[DAY])
    equity = _equity(db, backdated, "system")
    assert equity == pytest.approx(list(whole.equity), rel=0, abs=0)
    assert [x.hex() for x in equity] == [float(x).hex() for x in whole.equity]
    assert len(equity) == 16 and whole.equity.iloc[-1] != whole.equity.iloc[0]  # 期间有成交


def test_disabled_user_gets_no_signal_mail(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub = subscribe(db, "a@example.com", day)
    user = db.get(User, sub.user_id)
    assert user is not None
    user.status = "disabled"
    db.commit()
    assert run_day(env.ctx, day, True, False) == "done"
    assert processed_through(db, sub, "manual") == day
    assert env.sender.sent == []


def test_failed_calendar_refresh_alerts_when_stored_calendar_is_stale(
    env: Env, db: Session
) -> None:
    subscribe(db, "a@example.com", env.days[-1] + timedelta(days=30))  # 没有要补的日子
    env.source = mirror_source(env.store, NoCalendarSource)
    env.clock.now = evening(env.days[-1] + timedelta(days=10))
    assert catch_up(env.ctx) == []
    assert [m.subject for m in env.sender.sent] == ["【系统异常】交易日历更新失败"]
    env.clock.now = evening(env.days[-1])  # 日历还覆盖今天：不告警
    env.sender.sent.clear()
    assert catch_up(env.ctx) == []
    assert env.sender.sent == []


def test_weekly_holding_with_unknown_cost_shows_dash() -> None:
    report = WeeklyView(
        week_start=date(2022, 1, 3),
        manual_equity=1.0,
        system_equity=1.0,
        manual_week_return=0.0,
        system_week_return=0.0,
        benchmark_week_return=None,
        holdings=[HoldingView("600001", "甲", 100, None, 10.0, None)],
        signal_lines=[],
        trade_lines=[],
    )
    text = render_weekly(date(2022, 1, 7), report).text
    assert "成本：—" in text and "浮动盈亏：—" in text


# ---------- 持仓股票的日线下载失败 ----------


class FlakyBarsSource(FakeSource):
    """bars 对 failures 里的股票前若干次抛异常（模拟网络抖动），之后正常返回；
    broken_dividends 里的股票的分红接口每次都抛异常。"""

    failures: dict[str, int]
    broken_dividends: frozenset[str] = frozenset()

    def dividends(self, symbol: str) -> pd.DataFrame:
        if symbol in self.broken_dividends:
            self.calls.append(("dividends", (symbol,)))
            raise RuntimeError(f"模拟分红接口出错：{symbol}")
        return super().dividends(symbol)

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        left = self.failures.get(symbol, 0)
        if left > 0:
            self.failures[symbol] = left - 1
            self.calls.append(("bars", (symbol, start, end)))
            raise RuntimeError(f"模拟下载失败：{symbol}")
        return super().bars(symbol, start, end)


def _holding_whose_bar_is_missing(
    env: Env,
    db: Session,
    failures: int,
    source_has_bar: bool = True,
    dividends_broken: bool = False,
) -> tuple[Subscription, FlakyBarsSource]:
    """手动账持有 600001；本地日线只到前一个交易日，数据源按 failures 次数失败。"""
    days = env.days
    day = days[DAY]
    _add_bystander(env.store)  # 5 只股票：缺一只时覆盖率仍有 80%
    sub = subscribe(db, "a@example.com", day)
    panel = build_panel(env.store, env.calendar, CFG, days[0], day)
    market = StoreMarket(env.store, panel, CFG)
    record_holding(
        db, sub.user_id, sub.id, market, env.calendar, CFG, day, "600001", days[DAY - 5], 10.0, 1000
    )
    db.commit()
    full = env.store.read(Table.BARS)
    bars = (
        full
        if source_has_bar
        else full[~((full["symbol"] == "600001") & (full["day"] == pd.Timestamp(day)))]
    )
    source = FlakyBarsSource(
        trade_days=env.store.read(Table.TRADE_DAYS),
        securities=env.store.read(Table.SECURITIES),
        bars=bars.reset_index(drop=True),
        dividends=env.store.read(Table.DIVIDENDS),
        financials=env.store.read(Table.FINANCIALS),
        industries=env.store.read(Table.INDUSTRIES),
    )
    source.failures = {"600001": failures}
    if dividends_broken:  # 分红文件已过期，当晚的更新会去下载它
        source.broken_dividends = frozenset({"600001"})
        stale = evening(day) - timedelta(days=60)
        env.store.set_modified_time(Table.DIVIDENDS, "600001", stale)
        env.store.set_modified_time(Table.FINANCIALS, "600001", stale)
    env.source = source
    own = env.store.read(Table.BARS, symbols=["600001"])
    kept = own[own["day"] < pd.Timestamp(day)].reset_index(drop=True)
    env.store.write(Table.BARS, kept, symbol="600001")
    return sub, source


def test_holding_with_failed_download_fails_the_run(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub, source = _holding_whose_bar_is_missing(env, db, failures=99)
    assert run_day(env.ctx, day, True, True) == "data_failed"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Signal)) == 0
    assert processed_through(db, sub, "manual") is None
    run = daily_run(db, day)
    assert run is not None and "600001" in str(run.detail["problems"])
    assert "600001" in run.detail["download_failed"]
    subjects = [m.subject for m in env.sender.sent]
    assert all(m.to_addr == ADMIN for m in env.sender.sent)
    assert "【系统异常】持仓股票行情缺失" in subjects
    assert "【系统异常】部分股票下载失败" in subjects
    assert len([c for c in source.calls if c[0] == "bars" and c[1][0] == "600001"]) == 2


def test_holding_download_retried_once_then_run_is_normal(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub, _ = _holding_whose_bar_is_missing(env, db, failures=1)
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    bars = env.store.read(Table.BARS, symbols=["600001"])
    assert pd.Timestamp(day) in set(bars["day"])
    run = daily_run(db, day)
    assert run is not None and "600001" in run.detail["download_failed"]
    assert any(m.subject == "【系统异常】部分股票下载失败" for m in env.sender.sent)
    assert any(m.to_addr == "a@example.com" for m in env.sender.sent)


def test_suspended_holding_without_download_failure_runs_normally(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub, _ = _holding_whose_bar_is_missing(env, db, failures=0, source_has_bar=False)
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    run = daily_run(db, day)
    assert run is not None and run.detail["download_failed"] == {}
    assert not [m for m in env.sender.sent if m.to_addr == ADMIN]


# ---------- 补跑日子里的风控事件与放弃提醒 ----------


def test_abandon_mail_for_catch_up_day_is_sent_on_next_notified_night(
    env: Env, db: Session
) -> None:
    days = env.days
    subscribe(db, "a@example.com", days[DAY])
    assert run_day(env.ctx, days[DAY], True, False) == "done"  # 四个买入信号，有效期 3 个交易日
    env.sender.sent.clear()
    env.clock.now = evening(days[DAY + 5])
    assert [st for _, st in catch_up(env.ctx)] == ["done"] * 5  # DAY+4 放弃，那晚不通知
    abandon = [m for m in env.sender.sent if m.subject.startswith("【信号已过期】")]
    assert len(abandon) == 1 and f"截至 {days[DAY + 4]}" in abandon[0].text
    env.sender.sent.clear()
    env.clock.now = evening(days[DAY + 6])
    assert catch_up(env.ctx) == [(days[DAY + 6], "done")]
    assert not [m for m in env.sender.sent if m.subject.startswith("【信号已过期】")]


def test_risk_event_from_backdated_entry_is_mailed_next_night(env: Env, db: Session) -> None:
    days = env.days
    sub = subscribe(
        db, "a@example.com", days[DAY], {"risk.pause_drawdown": 0.003, "risk.warn_drawdown": 0.001}
    )
    assert run_day(env.ctx, days[DAY], True, False) == "done"
    assert run_day(env.ctx, days[DAY + 1], True, False) == "done"
    env.sender.sent.clear()
    panel = build_panel(env.store, env.calendar, CFG, days[0], days[DAY + 1])
    market = StoreMarket(env.store, panel, CFG)
    # DAY+1 已处理完之后才录入当天一笔高于收盘价的买入：重写派生表补出 DAY+1 的暂停事件
    record_trade(
        db,
        sub.user_id,
        sub.id,
        market,
        env.calendar,
        CFG,
        days[DAY + 1],
        Side.BUY,
        "600001",
        days[DAY + 1],
        10.5,
        3000,
    )
    db.commit()
    assert run_day(env.ctx, days[DAY + 2], True, False) == "done"
    risk = [m for m in env.sender.sent if m.subject.startswith("【风控提醒】")]
    assert len(risk) == 1 and str(days[DAY + 1]) in risk[0].text and "暂停买入" in risk[0].subject
    env.sender.sent.clear()
    assert run_day(env.ctx, days[DAY + 3], True, False) == "done"
    assert not [m for m in env.sender.sent if m.subject.startswith("【风控提醒】")]


def test_rerunning_a_past_day_updates_data_to_the_latest_closed_day(env: Env, db: Session) -> None:
    days = env.days
    subscribe(db, "a@example.com", days[DAY])
    env.clock.now = evening(days[DAY + 3])
    source = mirror_source(env.store)
    env.source = source
    assert run_day(env.ctx, days[DAY], True, True) == "done"
    ends = {args[2] for name, args in source.calls if name in ("benchmark", "bars")}
    assert ends == {days[DAY + 3]}  # 不按过去的那天截断基准、不把 ST 变化记到过去


def test_suspended_holding_with_failing_fundamentals_runs_normally(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub, source = _holding_whose_bar_is_missing(
        env, db, failures=0, source_has_bar=False, dividends_broken=True
    )
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    assert ("dividends", ("600001",)) in source.calls  # 分红接口确实在更新时出错
    run = daily_run(db, day)
    assert run is not None and "600001" in run.detail["download_failed"]  # 仍记录并告警
    assert any(m.subject == "【系统异常】部分股票下载失败" for m in env.sender.sent)


def test_bar_failure_cleared_by_clean_retry_is_treated_as_suspension(env: Env, db: Session) -> None:
    day = env.days[DAY]
    sub, _ = _holding_whose_bar_is_missing(env, db, failures=1, source_has_bar=False)
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    run = daily_run(db, day)
    assert run is not None and "600001" in run.detail["download_failed"]


def test_pending_system_buy_order_with_failed_bar_download_fails_the_run(
    env: Env, db: Session
) -> None:
    days = env.days
    _add_bystander(env.store)
    sub = subscribe(db, "a@example.com", days[DAY])
    assert run_day(env.ctx, days[DAY], True, False) == "done"  # 系统账挂出买单，次日开盘成交
    db.rollback()
    system = repo.get_book_of_kind(db, sub.user_id, sub.id, "system")
    state_row = repo.get_system_state(db, sub.user_id, system.id)
    assert state_row is not None
    pending, _ = load_state(state_row.state)
    assert "600001" in {o.symbol for o in pending.buy_orders} and not pending.positions
    manual = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    for sig in repo.list_signals(db, sub.user_id, manual.id, "buy"):
        if sig.symbol == "600001":  # 只留系统账的挂单这一个理由
            sig.status = "voided"
    db.commit()
    nxt = days[DAY + 1]
    source = FlakyBarsSource(
        trade_days=env.store.read(Table.TRADE_DAYS),
        securities=env.store.read(Table.SECURITIES),
        bars=env.store.read(Table.BARS),
        dividends=env.store.read(Table.DIVIDENDS),
        financials=env.store.read(Table.FINANCIALS),
        industries=env.store.read(Table.INDUSTRIES),
    )
    source.failures = {"600001": 99}
    env.source = source
    own = env.store.read(Table.BARS, symbols=["600001"])
    env.store.write(
        Table.BARS, own[own["day"] < pd.Timestamp(nxt)].reset_index(drop=True), symbol="600001"
    )
    env.clock.now = evening(nxt)
    assert run_day(env.ctx, nxt, True, True) == "data_failed"
    retried = [c for c in source.calls if c[0] == "bars" and c[1][0] == "600001"]
    assert len(retried) == 2  # 更新一次、再下载一次
    assert processed_through(db, sub, "system") == days[DAY]
    assert processed_through(db, sub, "manual") == days[DAY]
    run = daily_run(db, nxt)
    assert run is not None and "600001" in str(run.detail["problems"])


def test_catch_up_check_failure_keeps_download_failures_in_detail(env: Env, db: Session) -> None:
    days = env.days
    sub = subscribe(db, "a@example.com", days[DAY])
    _drop_bars(env.store, ["600001", "600002"], days[DAY])  # 当日覆盖率 50%：检查不通过
    source = mirror_source(env.store, FlakyBarsSource)
    assert isinstance(source, FlakyBarsSource)
    source.failures = {}
    source.fail_symbols = {"600003"}
    own = env.store.read(Table.BARS, symbols=["600003"])
    env.store.write(
        Table.BARS,
        own[own["day"] < pd.Timestamp(days[DAY])].reset_index(drop=True),
        symbol="600003",
    )
    env.source = source
    env.clock.now = evening(days[DAY])
    assert catch_up(env.ctx) == [(days[DAY], "data_failed")]
    run = daily_run(db, days[DAY])
    assert run is not None and run.stage == "check"
    assert "600003" in run.detail["download_failed"]
    assert processed_through(db, sub, "system") is None


# ---------- 批量行情 ----------


def _truncate_bars(store: ParquetStore, day: date) -> None:
    """库里每只股票的日线只留 day 之前的。"""
    for symbol in sorted(set(store.read(Table.SECURITIES)["symbol"])):
        own = store.read(Table.BARS, symbols=[symbol])
        store.write(Table.BARS, own[own["day"] < pd.Timestamp(day)].reset_index(drop=True), symbol)


def test_nightly_update_uses_bulk_snapshot_and_records_it(env: Env, db: Session) -> None:
    days = env.days
    day = days[DAY]
    subscribe(db, "a@example.com", day)
    full = env.store.read(Table.BARS)
    source = mirror_source(env.store)
    source.snapshot = snapshot_from_bars(full, day, days[DAY - 1])
    _truncate_bars(env.store, day)
    env.source = source
    assert run_day(env.ctx, day, True, True) == "done"
    assert [args for name, args in source.calls if name == "bars"] == []
    stored = env.store.read(Table.BARS, start=day, end=day).reset_index(drop=True)
    expected = full[full["day"] == pd.Timestamp(day)].reset_index(drop=True)
    pd.testing.assert_frame_equal(stored, expected, check_exact=True)
    run = daily_run(db, day)
    assert run is not None
    assert run.detail["bulk"] == {
        "used": True,
        "bars": 4,
        "suspended": 0,
        "per_symbol": 0,
        "reason": "",
    }


def test_nightly_update_records_why_bulk_was_not_used(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    source = mirror_source(env.store)  # 不模拟批量行情
    _truncate_bars(env.store, day)
    env.source = source
    assert run_day(env.ctx, day, True, True) == "done"
    run = daily_run(db, day)
    assert run is not None
    bulk = run.detail["bulk"]
    assert (bulk["used"], bulk["bars"], bulk["per_symbol"]) == (False, 0, 4)
    assert "批量行情获取失败" in bulk["reason"]


_EXTRA = [f"6000{i:02d}" for i in range(6, 12)]  # 再加 6 只旁观股票：共 11 只


def _holding_suspended_in_snapshot(
    env: Env, db: Session, source_has_bar: bool
) -> tuple[Subscription, FakeSource]:
    """手动账持有 600001；批量行情显示它停牌（全 0，昨收对得上），其余 10 只正常成交。

    库里每只股票的日线都只到前一个交易日；source_has_bar 决定逐只接口有没有 600001 当天的日线。
    1/11 无成交，不超过整晚闸门的 10%。
    """
    days = env.days
    day = days[DAY]
    sub, _ = _holding_whose_bar_is_missing(env, db, failures=0)
    template = env.store.read(Table.BARS, symbols=["600005"])  # 600001 的完整日线副本
    for symbol in _EXTRA:
        copy = template.copy()
        copy["symbol"] = symbol
        env.store.write(Table.BARS, copy, symbol=symbol)
    symbols = [*TRADING_SYMBOLS, "600005", *_EXTRA]
    env.store.write(
        Table.SECURITIES, make_securities([(s, "sh_main", date(2010, 1, 1), None) for s in symbols])
    )
    env.store.write(Table.INDUSTRIES, make_industries([(s, "银行", False) for s in symbols]))
    own = template.copy()
    own["symbol"] = "600001"
    others = env.store.read(Table.BARS, symbols=[s for s in symbols if s != "600001"])
    market = pd.concat([others, own], ignore_index=True)
    is_held_today = (market["symbol"] == "600001") & (market["day"] == pd.Timestamp(day))
    source = FakeSource(
        trade_days=env.store.read(Table.TRADE_DAYS),
        securities=env.store.read(Table.SECURITIES),
        bars=(market if source_has_bar else market[~is_held_today]).reset_index(drop=True),
        dividends=env.store.read(Table.DIVIDENDS),
        financials=env.store.read(Table.FINANCIALS),
        industries=env.store.read(Table.INDUSTRIES),
    )
    snapshot = snapshot_from_bars(market, day, days[DAY - 1])
    held = snapshot["symbol"] == "600001"
    for col in ("open", "high", "low", "close", "volume", "amount"):
        snapshot.loc[held, col] = 0.0
    source.snapshot = snapshot
    _truncate_bars(env.store, day)
    env.source = source
    return sub, source


def test_holding_suspended_in_snapshot_is_rechecked_and_treated_as_suspended(
    env: Env, db: Session
) -> None:
    day = env.days[DAY]
    sub, source = _holding_suspended_in_snapshot(env, db, source_has_bar=False)
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    # 更新阶段没有逐只请求；持仓缺当日日线，由行情检查再请求一次，数据源回答没有 → 停牌
    assert [args[0] for name, args in source.calls if name == "bars"] == ["600001"]
    run = daily_run(db, day)
    assert run is not None
    bulk = run.detail["bulk"]
    assert (bulk["used"], bulk["bars"], bulk["suspended"], bulk["per_symbol"]) == (True, 10, 1, 0)
    assert run.detail["download_failed"] == {}
    assert run.detail["bars_retried"] == {
        "symbols": ["600001"],
        "still_missing": ["600001"],
        "failed": {},
    }
    assert not [m for m in env.sender.sent if m.to_addr == ADMIN]


def test_holding_wrongly_suspended_in_snapshot_gets_its_bar_from_the_recheck(
    env: Env, db: Session
) -> None:
    day = env.days[DAY]
    sub, source = _holding_suspended_in_snapshot(env, db, source_has_bar=True)
    assert run_day(env.ctx, day, True, True) == "done"
    assert processed_through(db, sub, "manual") == day
    assert [args[0] for name, args in source.calls if name == "bars"] == ["600001"]
    stored = env.store.read(Table.BARS, symbols=["600001"])
    assert stored["day"].max() == pd.Timestamp(day)
    run = daily_run(db, day)
    assert run is not None
    assert run.detail["bars_retried"] == {"symbols": ["600001"], "still_missing": [], "failed": {}}
