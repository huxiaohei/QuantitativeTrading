"""每日任务：数据、检查、面板、逐个使用者推进两本账与写邮件、发送；重跑、补跑与出错隔离。"""

from collections import Counter
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qt.backtest.report import run_period
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import Base, BookDay, Outbox, Signal, Subscription, Trade, User
from qt.features import FeaturePanel, build_panel
from qt.jobs import daily
from qt.jobs.daily import catch_up, pending_days, run_day
from qt.mail.render import HoldingView, WeeklyView, render_weekly
from qt.mail.send import Sender
from qt.service.accounts import close_subscription, pause_subscription, subscription_config
from qt.service.manual_book import Market
from qt.service.manual_ops import ManualDayResult, record_holding
from qt.service.market import StoreMarket
from tests.fakes import TRADING_SYMBOLS, FakeSource, make_industries, make_securities
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
    assert "手动账（请据此操作）" in mail.text and "系统账（自动执行，仅供对照）" in mail.text
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

    def killed(s: Session, sender: Sender, max_attempts: int = 5) -> tuple[int, int]:
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
    for _ in range(5):
        assert run_day(env.ctx, day, True, False) == "done"
    db.rollback()
    [dead] = db.scalars(select(Outbox).where(Outbox.to_addr == "a@example.com"))
    assert dead.status == "failed"
    alerts = [m for m in env.sender.sent if m.to_addr == ADMIN]
    assert [m.subject for m in alerts] == ["【系统异常】邮件发送失败"]
    assert "a@example.com" in alerts[0].text
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
    """bars 对 failures 里的股票前若干次抛异常（模拟网络抖动），之后正常返回。"""

    failures: dict[str, int]

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        left = self.failures.get(symbol, 0)
        if left > 0:
            self.failures[symbol] = left - 1
            self.calls.append(("bars", (symbol, start, end)))
            raise RuntimeError(f"模拟下载失败：{symbol}")
        return super().bars(symbol, start, end)


def _holding_whose_bar_is_missing(
    env: Env, db: Session, failures: int, source_has_bar: bool = True
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
