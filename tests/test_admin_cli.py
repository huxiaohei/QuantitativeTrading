"""管理命令：用 Typer 的 CliRunner 对测试库与假数据（临时目录）执行，不联网、不碰真实 data/。

命令通过环境变量找到数据库、配置与数据目录（DATABASE_URL、QT_CONFIG、QT_DATA_DIR），
与 qt run-day、qt worker 共用 qt.runtime 的解析；当前时间通过替换 qt.admin_cli.now 控制。
"""

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

import qt.admin_cli
import qt.cli
from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.db import admin_repo, repo
from qt.db.models import (
    AuditLog,
    DailyRun,
    Followup,
    LedgerEntry,
    Outbox,
    Signal,
    Subscription,
    User,
)
from qt.features import build_panel
from qt.runtime import Runtime
from qt.service import accounts
from qt.service.accounts import create_subscription, create_user
from qt.service.errors import ValidationError
from qt.service.manual_ops import advance_manual_book
from qt.service.market import StoreMarket
from tests.conftest import resolve_test_db_url
from tests.fakes import populate_trading_store

_ROOT = Path(__file__).resolve().parents[1]
CFG: AppConfig = load_config(_ROOT / "config" / "default.yaml")
SH = ZoneInfo("Asia/Shanghai")
CASH = 100_000.0
S = 260  # 2022-01-03：订阅起始日，四只股票都满足买入条件
EMAIL = "a@x.com"

type Invoke = Callable[..., Result]


class SetNow(Protocol):
    def __call__(self, day: date, hour: int = 9) -> None: ...


@dataclass(frozen=True)
class World:
    store: ParquetStore
    days: list[date]
    calendar: TradingCalendar


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine: Engine, db: Session) -> World:
    store = ParquetStore(tmp_path / "data")
    days = populate_trading_store(store)
    monkeypatch.setenv("QT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QT_CONFIG", str(_ROOT / "config" / "default.yaml"))
    monkeypatch.setenv("DATABASE_URL", resolve_test_db_url())
    return World(store, days, TradingCalendar(days))


@pytest.fixture
def set_now(monkeypatch: pytest.MonkeyPatch) -> SetNow:
    def _set(day: date, hour: int = 9) -> None:
        moment = datetime.combine(day, time(hour, 0), tzinfo=SH)

        def fake_now() -> datetime:
            return moment

        monkeypatch.setattr(qt.admin_cli, "now", fake_now)

    return _set


@pytest.fixture
def invoke() -> Invoke:
    def _invoke(*args: str) -> Result:
        return CliRunner().invoke(qt.cli.app, list(args))

    return _invoke


def _subscribe(db: Session, w: World, email: str = EMAIL, start: int = S) -> Subscription:
    user = create_user(db, email)
    sub = create_subscription(db, user.id, CFG, CASH, w.days[start])
    db.commit()
    return sub


def _nights(db: Session, w: World, sub: Subscription, first: int, last: int) -> None:
    """用服务层推进手动账的若干个晚上（造出信号与派生表）。"""
    for i in range(first, last + 1):
        panel = build_panel(w.store, w.calendar, CFG, w.days[i], w.days[i])
        market = StoreMarket(w.store, panel, CFG)
        fresh = repo.get_subscription(db, sub.user_id, sub.id)
        advance_manual_book(db, fresh, CFG, panel, market, w.calendar, w.days[i], True)
        db.commit()


def _ledger(db: Session, sub: Subscription) -> list[LedgerEntry]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    return repo.list_ledger_entries(db, sub.user_id, book.id)


# ---------- 使用者与订阅 ----------


def test_user_and_subscription_lifecycle_via_cli(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    set_now(world.days[262])  # 周三晚；下一个交易日是 days[263]
    assert invoke("admin", "user", "add", "--email", "A@X.com").exit_code == 0
    r = invoke("admin", "user", "list")
    assert r.exit_code == 0 and "a@x.com" in r.stdout and "无订阅" in r.stdout

    r = invoke(
        "admin",
        "sub",
        "create",
        "--email",
        EMAIL,
        "--cash",
        "150000",
        "--set",
        "strategy.stop_loss=0.10",
    )
    assert r.exit_code == 0, r.stdout
    assert str(world.days[263]) in r.stdout and "150,000.00" in r.stdout
    shown = invoke("admin", "sub", "show", "--email", EMAIL)
    assert shown.exit_code == 0
    assert "止损" in shown.stdout
    stop_line = next(line for line in shown.stdout.splitlines() if "strategy.stop_loss" in line)
    assert "：0.1 ←" in stop_line and "默认 0.15" in stop_line
    other = next(line for line in shown.stdout.splitlines() if "strategy.take_profit_fixed" in line)
    assert "默认" not in other  # 与默认值相同的不标出
    assert "有生效订阅" not in shown.stdout
    assert "生效中" in invoke("admin", "user", "list").stdout

    user = admin_repo.find_user_by_email(db, EMAIL)
    assert user is not None
    sub = repo.get_open_subscription(db, user.id)
    assert sub is not None and sub.start_day == world.days[263] and sub.initial_cash == 150_000.0

    r = invoke(
        "admin",
        "sub",
        "set",
        "--email",
        EMAIL,
        "--set",
        "risk.max_positions=5",
        "--set",
        "strategy.take_profit_mode=trailing",
    )
    assert r.exit_code == 0 and "下一次运行" in r.stdout
    db.rollback()
    db.refresh(sub)
    assert sub.params["risk"]["max_positions"] == 5
    assert sub.params["strategy"]["take_profit_mode"] == "trailing"
    assert "与默认值不同" in invoke("admin", "sub", "show", "--email", EMAIL).stdout

    assert invoke("admin", "sub", "pause", "--email", EMAIL).exit_code == 0
    assert "已暂停" in invoke("admin", "user", "list").stdout
    assert invoke("admin", "sub", "resume", "--email", EMAIL).exit_code == 0
    assert invoke("admin", "sub", "close", "--email", EMAIL).exit_code == 0
    assert "无订阅" in invoke("admin", "user", "list").stdout
    r = invoke("admin", "sub", "resume", "--email", EMAIL)
    assert r.exit_code == 1  # 没有未关闭的订阅
    r = invoke("admin", "sub", "show", "--email", EMAIL)  # 已关闭的订阅仍可查看
    assert r.exit_code == 0 and "已关闭" in r.stdout


def test_sub_create_twice_and_bad_input_are_business_errors(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    set_now(world.days[262])
    invoke("admin", "user", "add", "--email", EMAIL)
    ok = invoke("admin", "sub", "create", "--email", EMAIL, "--cash", "1000")
    assert ok.exit_code == 0
    again = invoke("admin", "sub", "create", "--email", EMAIL, "--cash", "1000")
    assert again.exit_code == 1 and "已有生效的订阅" in again.stdout
    assert "Traceback" not in again.stdout
    assert invoke("admin", "user", "add", "--email", EMAIL).exit_code == 1  # 重复
    bad = invoke("admin", "user", "add", "--email", "no at sign")
    assert bad.exit_code == 1 and "邮箱格式" in bad.stdout
    malformed = invoke("admin", "sub", "set", "--email", EMAIL, "--set", "strategy.stop_loss")
    assert malformed.exit_code == 1 and "路径=值" in malformed.stdout
    nothing = invoke("admin", "sub", "set", "--email", EMAIL)
    assert nothing.exit_code == 1 and "--set" in nothing.stdout
    out_of_range = invoke("admin", "sub", "set", "--email", EMAIL, "--set", "strategy.stop_loss=5")
    assert out_of_range.exit_code == 1 and "Traceback" not in out_of_range.stdout


def test_user_weekly_switch(world: World, db: Session, invoke: Invoke) -> None:
    invoke("admin", "user", "add", "--email", EMAIL)
    assert invoke("admin", "user", "weekly", "--email", EMAIL, "--off").exit_code == 0
    user = admin_repo.find_user_by_email(db, EMAIL)
    assert user is not None
    db.refresh(user)
    assert user.weekly_mail is False
    assert "周报：关" in invoke("admin", "user", "list").stdout
    assert invoke("admin", "user", "weekly", "--email", EMAIL, "--on").exit_code == 0
    db.refresh(user)
    assert user.weekly_mail is True
    assert invoke("admin", "user", "weekly", "--email", "nobody@x.com", "--on").exit_code == 1


def test_user_add_admin_flag(world: World, db: Session, invoke: Invoke) -> None:
    assert invoke("admin", "user", "add", "--email", "boss@x.com", "--admin").exit_code == 0
    user = admin_repo.find_user_by_email(db, "boss@x.com")
    assert user is not None and user.is_admin is True
    assert "管理员" in invoke("admin", "user", "list").stdout


# ---------- 录入 ----------


def test_trade_commands_record_and_show_in_book(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    set_now(world.days[262])
    # 与邮件里打印的命令形态一致：SYMBOL 为位置参数，--day 缺省为今天之前最近的交易日
    r = invoke(
        "admin", "trade", "buy", "--email", EMAIL, "600001", "--price", "10", "--shares", "1000"
    )
    assert r.exit_code == 0, r.stdout
    entry = _ledger(db, sub)[-1]
    assert entry.kind == "buy" and entry.trade_day == world.days[261]
    assert entry.shares == 1000 and entry.price == 10 and entry.fees > 0 and entry.fees_estimated
    assert "自主买入" in r.stdout

    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert shown.exit_code == 0, shown.stdout
    assert "600001" in shown.stdout and "自主买入" in shown.stdout
    assert "手动账" in shown.stdout

    r = invoke(
        "admin",
        "trade",
        "sell",
        "--email",
        EMAIL,
        "600001",
        "--price",
        "10",
        "--shares",
        "400",
        "--day",
        str(world.days[262]),
        "--fees",
        "3.5",
    )
    assert r.exit_code == 0, r.stdout
    sold = _ledger(db, sub)[-1]
    assert sold.kind == "sell" and sold.fees == 3.5 and not sold.fees_estimated
    assert sold.trade_day == world.days[262]
    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert "600" in shown.stdout and "卖出" in shown.stdout


def test_buy_linked_to_signal_shows_signal_origin(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    _nights(db, world, sub, S, S)
    signal = db.scalars(select(Signal).where(Signal.symbol == "600001")).one()
    set_now(world.days[262])
    r = invoke(
        "admin", "trade", "buy", "--email", EMAIL, "600001", "--price", "10", "--shares", "900"
    )
    assert r.exit_code == 0, r.stdout
    assert f"信号 #{signal.id}" in r.stdout and "按信号买入" in r.stdout
    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert "按信号买入" in shown.stdout and "自主买入" not in shown.stdout


def test_holding_and_cash_commands(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    set_now(world.days[262])
    r = invoke(
        "admin",
        "holding",
        "add",
        "--email",
        EMAIL,
        "600003",
        "--cost",
        "9.5",
        "--shares",
        "2000",
        "--bought",
        "2021-12-01",
    )
    assert r.exit_code == 0, r.stdout
    assert _ledger(db, sub)[-1].kind == "transfer_in"
    r = invoke("admin", "cash", "deposit", "--email", EMAIL, "--amount", "20000")
    assert r.exit_code == 0, r.stdout
    assert "20,000.00" in r.stdout
    r = invoke(
        "admin",
        "cash",
        "withdraw",
        "--email",
        EMAIL,
        "--amount",
        "5000.5",
        "--day",
        str(world.days[261]),
    )
    assert r.exit_code == 0, r.stdout
    kinds = [e.kind for e in _ledger(db, sub)]
    assert kinds == ["transfer_in", "deposit", "withdraw"]
    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert "600003" in shown.stdout and "自主买入" in shown.stdout
    assert "入金" in shown.stdout and "出金" in shown.stdout and "转入" in shown.stdout
    assert "5,000.50" in shown.stdout
    too_much = invoke("admin", "cash", "withdraw", "--email", EMAIL, "--amount", "9999999")
    assert too_much.exit_code == 1 and "Traceback" not in too_much.stdout


def test_business_errors_print_chinese_message_and_exit_1_without_traceback(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    _subscribe(db, world)
    set_now(world.days[262])
    assert (
        invoke(
            "admin", "trade", "buy", "--email", EMAIL, "600001", "--price", "10", "--shares", "1000"
        ).exit_code
        == 0
    )
    r = invoke(
        "admin", "trade", "sell", "--email", EMAIL, "600001", "--price", "10", "--shares", "999999"
    )
    assert r.exit_code == 1 and "超过持仓" in r.stdout and "Traceback" not in r.stdout
    r = invoke("admin", "sub", "set", "--email", EMAIL, "--set", "strategy.lookback_days=100")
    assert r.exit_code == 1 and "全站统一" in r.stdout and "Traceback" not in r.stdout
    assert invoke("admin", "sub", "show", "--email", "nobody@x.com").exit_code == 1
    r = invoke(
        "admin", "trade", "buy", "--email", EMAIL, "999999", "--price", "10", "--shares", "100"
    )
    assert r.exit_code == 1 and "股票代码不存在" in r.stdout
    r = invoke(
        "admin",
        "trade",
        "buy",
        "--email",
        EMAIL,
        "600001",
        "--price",
        "10",
        "--shares",
        "100",
        "--day",
        "not-a-date",
    )
    assert r.exit_code == 1 and "YYYY-MM-DD" in r.stdout
    r = invoke("admin", "entry", "void", "--email", EMAIL, "99999")
    assert r.exit_code == 1 and "Traceback" not in r.stdout


def test_failed_command_leaves_no_rows(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    set_now(world.days[262])
    before = db.scalars(select(AuditLog.id)).all()
    r = invoke(
        "admin", "trade", "sell", "--email", EMAIL, "600001", "--price", "10", "--shares", "100"
    )
    assert r.exit_code == 1
    assert _ledger(db, sub) == []
    assert db.scalars(select(AuditLog.id)).all() == before


def test_void_command_and_derived_dividend_rows_in_book_show(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    buy_day = date(2022, 6, 14)  # 600001 在 2022-06-15 除权，每股 0.6 元
    assert buy_day in world.days
    set_now(date(2022, 6, 22))
    r = invoke(
        "admin",
        "trade",
        "buy",
        "--email",
        EMAIL,
        "600001",
        "--price",
        "13.5",
        "--shares",
        "1000",
        "--day",
        str(buy_day),
    )
    assert r.exit_code == 0, r.stdout
    entry_id = _ledger(db, sub)[-1].id
    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert shown.exit_code == 0, shown.stdout
    dividend = next(line for line in shown.stdout.splitlines() if "分红" in line)
    assert "600001" in dividend and "600.00" in dividend and "推算" in dividend

    r = invoke("admin", "entry", "void", "--email", EMAIL, str(entry_id))
    assert r.exit_code == 0, r.stdout
    shown = invoke("admin", "book", "show", "--email", EMAIL)
    assert "已作废" in shown.stdout and "作废流水" in shown.stdout
    assert not any("分红" in line for line in shown.stdout.splitlines())
    assert "自主买入" not in shown.stdout  # 持仓已空（流水里的来源标注不同）
    again = invoke("admin", "entry", "void", "--email", EMAIL, str(entry_id))
    assert again.exit_code == 1 and "已作废" in again.stdout


def test_book_show_system_book_and_recent_entry_limit(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    set_now(world.days[270])
    for _ in range(25):
        r = invoke(
            "admin",
            "cash",
            "deposit",
            "--email",
            EMAIL,
            "--amount",
            "100",
            "--day",
            str(world.days[269]),
        )
        assert r.exit_code == 0, r.stdout
    assert len(_ledger(db, sub)) == 25
    shown = invoke("admin", "book", "show", "--email", EMAIL, "--book", "manual")
    lines = [line for line in shown.stdout.splitlines() if "入金" in line]
    assert len(lines) == 20
    system = invoke("admin", "book", "show", "--email", EMAIL, "--book", "system")
    assert system.exit_code == 0 and "系统账" in system.stdout and "尚未推进" in system.stdout
    bad = invoke("admin", "book", "show", "--email", EMAIL, "--book", "other")
    assert bad.exit_code != 0


def test_book_show_system_book_after_nightly_run(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    from qt.service.system_book import advance_system_book

    sub = _subscribe(db, world)
    for i in range(S, S + 4):
        panel = build_panel(world.store, world.calendar, CFG, world.days[i], world.days[i])
        advance_system_book(
            db,
            repo.get_subscription(db, sub.user_id, sub.id),
            CFG,
            panel,
            world.calendar,
            world.days[i],
        )
        db.commit()
    set_now(world.days[S + 4])
    shown = invoke("admin", "book", "show", "--email", EMAIL, "--book", "system")
    assert shown.exit_code == 0, shown.stdout
    assert "系统账" in shown.stdout and "按信号买入" in shown.stdout
    assert "60000" in shown.stdout


# ---------- 查看 ----------


def test_signals_and_journal_commands(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    _nights(db, world, sub, S, S + 2)
    set_now(world.days[S + 3])
    assert (
        invoke(
            "admin",
            "trade",
            "buy",
            "--email",
            EMAIL,
            "600001",
            "--price",
            "10",
            "--shares",
            "900",
            "--day",
            str(world.days[S + 1]),
        ).exit_code
        == 0
    )
    assert (
        invoke(
            "admin", "trade", "sell", "--email", EMAIL, "600001", "--price", "10", "--shares", "900"
        ).exit_code
        == 0
    )

    r = invoke("admin", "signals", "--email", EMAIL)
    assert r.exit_code == 0, r.stdout
    assert "买入信号" in r.stdout and "600001" in r.stdout and "已执行" in r.stdout
    assert "已通知" in r.stdout  # 其他股票的信号仍在通知状态
    only = invoke("admin", "signals", "--email", EMAIL, "--status", "executed")
    assert "已执行" in only.stdout and "已通知" not in only.stdout
    chinese = invoke("admin", "signals", "--email", EMAIL, "--status", "已执行")
    assert chinese.exit_code == 0 and "已执行" in chinese.stdout
    assert invoke("admin", "signals", "--email", EMAIL, "--days", "0").stdout.count("600001") == 0
    assert invoke("admin", "signals", "--email", EMAIL, "--status", "bogus").exit_code == 1
    system = invoke("admin", "signals", "--email", EMAIL, "--book", "system")
    assert system.exit_code == 0 and "没有符合条件的信号" in system.stdout

    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    db.add(
        Followup(
            book_id=book.id,
            user_id=sub.user_id,
            symbol="600001",
            event="exit",
            event_day=world.days[S + 3],
            base_adj_close=10.0,
            ret_20=0.05,
            ret_60=None,
        )
    )
    db.commit()
    j = invoke("admin", "journal", "--email", EMAIL)
    assert j.exit_code == 0, j.stdout
    assert "已完成的交易" in j.stdout and "600001" in j.stdout
    assert str(world.days[S + 3]) in j.stdout
    assert "跟踪记录" in j.stdout and "5.00%" in j.stdout and "待填" in j.stdout
    empty = invoke("admin", "journal", "--email", EMAIL, "--book", "system")
    assert empty.exit_code == 0 and "暂无" in empty.stdout


def test_runs_command_lists_failure_reason(world: World, db: Session, invoke: Invoke) -> None:
    empty = invoke("admin", "runs")
    assert empty.exit_code == 0 and "还没有运行记录" in empty.stdout
    started = datetime(2026, 11, 13, 18, 0, tzinfo=SH)
    later = datetime(2026, 11, 17, 18, 0, tzinfo=SH)
    db.add_all(
        [
            DailyRun(
                trade_day=date(2026, 11, 12),
                status="done",
                stage="done",
                started_at=started,
                finished_at=started,
                detail={},
            ),
            DailyRun(
                trade_day=date(2026, 11, 13),
                status="data_failed",
                stage="data",
                started_at=started,
                finished_at=started,
                detail={"error": "SharedTableError: 证券列表取不到"},
            ),
            DailyRun(
                trade_day=date(2026, 11, 16),
                status="done_with_errors",
                stage="done",
                started_at=later,
                finished_at=later,
                detail={
                    "user_errors": [
                        {
                            "user_id": 7,
                            "subscription_id": 3,
                            "error": "ValidationError: 参数已不符合当前规则",
                        }
                    ],
                    "mail_error": "OperationalError: 连接断开",
                },
            ),
        ]
    )
    db.commit()
    r = invoke("admin", "runs")
    assert r.exit_code == 0, r.stdout
    assert "数据异常" in r.stdout and "证券列表取不到" in r.stdout
    assert "参数已不符合当前规则" in r.stdout and "使用者 7" in r.stdout
    assert "连接断开" in r.stdout
    assert r.stdout.index("2026-11-16") < r.stdout.index("2026-11-13")  # 最近的在前
    last_one = invoke("admin", "runs", "--last", "1")
    assert "2026-11-16" in last_one.stdout and "2026-11-13" not in last_one.stdout


def _mail(db: Session, key: str, status: str, error: str | None = None) -> Outbox:
    row = Outbox(
        to_addr="u@x.com",
        kind="buy",
        dedupe_key=key,
        subject=f"主题 {key}",
        body_html="h",
        body_text="t",
        status=status,
        attempts=3 if status != "pending" else 0,
        last_error=error,
    )
    db.add(row)
    db.commit()
    return row


def test_outbox_list_and_retry(world: World, db: Session, invoke: Invoke, set_now: SetNow) -> None:
    set_now(world.days[S], 20)
    _mail(db, "k1", "sent")
    dead1 = _mail(db, "k2", "failed", "ConnectionError: 连接被拒绝")
    dead2 = _mail(db, "k3", "failed", "ConnectionError: 超时")
    _mail(db, "k4", "pending")
    listed = invoke("admin", "outbox", "list")
    assert listed.exit_code == 0, listed.stdout
    assert all(f"主题 {k}" in listed.stdout for k in ("k1", "k2", "k3", "k4"))
    failed = invoke("admin", "outbox", "list", "--status", "failed")
    assert "主题 k2" in failed.stdout and "主题 k1" not in failed.stdout
    assert "连接被拒绝" in failed.stdout
    bad = invoke("admin", "outbox", "list", "--status", "lost")
    assert bad.exit_code == 1
    r = invoke("admin", "outbox", "retry", str(dead1.id))
    assert r.exit_code == 0 and "重新排队" in r.stdout
    db.rollback()
    db.refresh(dead1)
    assert dead1.status == "pending"
    assert dead1.created_at == datetime.combine(world.days[S], time(20, 0), tzinfo=SH)
    again = invoke("admin", "outbox", "retry", str(dead1.id))
    assert again.exit_code == 1 and "只有发送失败" in again.stdout
    neither = invoke("admin", "outbox", "retry")
    assert neither.exit_code == 1
    everything = invoke("admin", "outbox", "retry", "--all-failed")
    assert everything.exit_code == 0 and "1 封" in everything.stdout
    db.rollback()
    db.refresh(dead2)
    assert dead2.status == "pending"


def test_runs_command_shows_outbox_counts(world: World, db: Session, invoke: Invoke) -> None:
    _mail(db, "k1", "pending")
    _mail(db, "k2", "pending")
    _mail(db, "k3", "failed", "boom")
    _mail(db, "k4", "sent")
    r = invoke("admin", "runs")
    assert r.exit_code == 0, r.stdout
    assert "发件箱：待发 2 封，发送失败 1 封" in r.stdout


# ---------- 错误处理 ----------


def test_inconsistent_ledger_exits_2_with_repair_hint(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, "manual")
    db.add(  # 直接写入一笔没有持仓的卖出：账本本身不一致
        LedgerEntry(
            book_id=book.id,
            user_id=sub.user_id,
            kind="sell",
            symbol="600001",
            trade_day=world.days[S + 1],
            effective_day=world.days[S + 1],
            shares=100,
            price=10.0,
            fees=0.0,
            fees_estimated=False,
            origin="self",
            created_by_user_id=sub.user_id,
            note="",
        )
    )
    db.commit()
    set_now(world.days[S + 3])
    for args in (
        ("book", "show", "--email", EMAIL),
        ("trade", "buy", "--email", EMAIL, "600002", "--price", "10", "--shares", "100"),
    ):
        r = invoke("admin", *args)
        assert r.exit_code == 2, r.stdout
        assert "不一致" in r.stdout and "需要修复" in r.stdout and "Traceback" not in r.stdout


def test_unexpected_errors_are_not_swallowed(
    world: World, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> User:
        raise KeyError("意外")

    monkeypatch.setattr(qt.admin_cli.accounts, "create_user", boom)
    r = invoke("admin", "user", "add", "--email", EMAIL)
    assert isinstance(r.exception, KeyError)


def test_missing_database_url_prints_message(
    world: World, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL")
    r = invoke("admin", "user", "list")
    assert r.exit_code == 1 and "DATABASE_URL" in r.stdout and "Traceback" not in r.stdout


def test_missing_calendar_prints_message(
    world: World, db: Session, invoke: Invoke, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _subscribe(db, world)
    monkeypatch.setenv("QT_DATA_DIR", str(tmp_path / "empty"))
    r = invoke("admin", "book", "show", "--email", EMAIL)
    assert r.exit_code == 1 and "trade_days" in r.stdout and "Traceback" not in r.stdout


def test_money_and_percent_formatting_helpers() -> None:
    assert qt.admin_cli.money(1234567.891) == "1,234,567.89"
    assert qt.admin_cli.money(-5) == "-5.00"
    assert qt.admin_cli.pct(0.123456) == "12.35%"
    assert qt.admin_cli.pct(-0.5) == "-50.00%"


def test_new_admin_repo_and_repo_queries(world: World, db: Session) -> None:
    sub = _subscribe(db, world)
    assert repo.get_latest_subscription(db, sub.user_id) is not None
    assert repo.get_latest_subscription(db, sub.user_id + 1000) is None
    assert admin_repo.list_recent_runs(db, 5) == []


# ---------- 与 run-day / worker 共用同一套解析 ----------


def test_admin_run_day_and_worker_resolve_same_config_and_data_dir(
    world: World,
    db: Session,
    invoke: Invoke,
    set_now: SetNow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "site.yaml"
    config.write_text(
        (_ROOT / "config" / "default.yaml")
        .read_text(encoding="utf-8")
        .replace("stop_loss: 0.15", "stop_loss: 0.12"),
        encoding="utf-8",
    )
    data_dir = tmp_path / "elsewhere"
    monkeypatch.setenv("QT_CONFIG", str(config))
    monkeypatch.setenv("QT_DATA_DIR", str(data_dir))

    seen: list[tuple[str, Path, Path]] = []

    class Spy(Runtime):
        def __init__(self, config: Path, data_dir: Path) -> None:
            seen.append(("admin", config, data_dir))
            super().__init__(config, data_dir)

    def spy_context(config: Path, data_dir: Path) -> object:
        seen.append(("job", config, data_dir))
        raise RuntimeError("测试中止")

    monkeypatch.setattr(qt.admin_cli, "Runtime", Spy)
    monkeypatch.setattr(qt.cli, "make_context", spy_context)
    invoke("admin", "user", "list")
    assert invoke("run-day", "--no-update").exit_code == 1
    assert invoke("worker").exit_code == 1
    assert seen == [
        ("admin", config, data_dir),
        ("job", config, data_dir),
        ("job", config, data_dir),
    ]

    # 命令行选项优先于环境变量，三者一致
    other = tmp_path / "other"
    seen.clear()
    invoke("admin", "--data-dir", str(other), "user", "list")
    invoke("run-day", "--data-dir", str(other))
    assert [(c, d) for _, c, d in seen] == [(config, other), (config, other)]


def test_admin_uses_config_from_qt_config_like_the_nightly_job(
    world: World,
    invoke: Invoke,
    set_now: SetNow,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "site.yaml"
    config.write_text(
        (_ROOT / "config" / "default.yaml")
        .read_text(encoding="utf-8")
        .replace("stop_loss: 0.15", "stop_loss: 0.12"),
        encoding="utf-8",
    )
    monkeypatch.setenv("QT_CONFIG", str(config))
    set_now(world.days[262])
    invoke("admin", "user", "add", "--email", EMAIL)
    assert invoke("admin", "sub", "create", "--email", EMAIL, "--cash", "1000").exit_code == 0
    shown = invoke("admin", "sub", "show", "--email", EMAIL).stdout
    assert "止损比例：0.12" in shown and "共 0 项与默认值不同" in shown


# ---------- --day 缺省：最近已收盘的交易日 ----------


def _deposit_day(db: Session, sub: Subscription, invoke: Invoke) -> date:
    r = invoke("admin", "cash", "deposit", "--email", EMAIL, "--amount", "100")
    assert r.exit_code == 0, r.stdout
    return _ledger(db, sub)[-1].trade_day


def test_default_day_is_latest_closed_trading_day(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    sub = _subscribe(db, world)
    wed = world.days[262]  # 2022-01-05 周三
    assert wed.weekday() == 2
    set_now(wed, 20)  # 交易日傍晚：就是今天
    assert _deposit_day(db, sub, invoke) == wed
    set_now(wed, 15)  # 15:00 整点算已收盘
    assert _deposit_day(db, sub, invoke) == wed
    set_now(wed, 14)  # 交易日下午三点前：上一个交易日
    assert _deposit_day(db, sub, invoke) == world.days[261]
    set_now(wed, 9)
    assert _deposit_day(db, sub, invoke) == world.days[261]
    saturday = date(2022, 1, 8)
    assert saturday not in world.days
    set_now(saturday, 20)  # 周末：周五
    assert _deposit_day(db, sub, invoke) == world.days[264]
    set_now(wed, 20)  # trade 用同一规则
    r = invoke(
        "admin", "trade", "buy", "--email", EMAIL, "600001", "--price", "10", "--shares", "100"
    )
    assert r.exit_code == 0, r.stdout
    assert _ledger(db, sub)[-1].trade_day == wed


def test_day_option_help_states_default_rule(invoke: Invoke) -> None:
    for args in (("trade", "buy"), ("trade", "sell"), ("cash", "deposit"), ("cash", "withdraw")):
        text = invoke("admin", *args, "--help").stdout
        assert "已收盘" in text and "15:00" in text


# ---------- 提交之后才打印；失败回滚 ----------


def test_failure_after_rows_were_flushed_rolls_everything_back(
    world: World,
    db: Session,
    invoke: Invoke,
    set_now: SetNow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = _subscribe(db, world)
    set_now(world.days[262])
    before = db.scalars(select(AuditLog.id)).all()
    real = repo.add_audit

    def flush_then_fail(
        s: Session, user_id: int, action: str, target: str, detail: Mapping[str, Any]
    ) -> AuditLog:
        real(s, user_id, action, target, detail)  # 流水与审计已 flush 进事务
        raise ValidationError("模拟的后续失败")

    monkeypatch.setattr(repo, "add_audit", flush_then_fail)
    r = invoke(
        "admin", "trade", "buy", "--email", EMAIL, "600001", "--price", "10", "--shares", "100"
    )
    assert r.exit_code == 1 and "模拟的后续失败" in r.stdout and "已录入" not in r.stdout
    assert _ledger(db, sub) == []
    assert db.scalars(select(AuditLog.id)).all() == before


def test_success_message_is_printed_only_after_commit(
    world: World, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = qt.admin_cli.session_scope

    @contextmanager
    def failing_commit(engine: Engine) -> Generator[Session]:
        with real(engine) as s:
            yield s
        raise ValidationError("模拟提交失败")

    monkeypatch.setattr(qt.admin_cli, "session_scope", failing_commit)
    r = invoke("admin", "user", "add", "--email", EMAIL)
    assert r.exit_code == 1 and "已创建" not in r.stdout and "模拟提交失败" in r.stdout


def _create_with_start(invoke: Invoke, *extra: str) -> Result:
    invoke("admin", "user", "add", "--email", EMAIL)
    return invoke("admin", "sub", "create", "--email", EMAIL, "--cash", "1000", *extra)


def _start_of(db: Session) -> date:
    db.rollback()
    user = db.scalars(select(User).where(User.email == EMAIL)).one()
    sub = repo.get_open_subscription(db, user.id)
    assert sub is not None
    return sub.start_day


def test_sub_create_start_defaults_to_next_trading_day(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    set_now(world.days[262])
    assert _create_with_start(invoke).exit_code == 0
    assert _start_of(db) == world.days[263]


def test_sub_create_accepts_a_past_trading_day_within_the_limit(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    set_now(world.days[262])
    limit = CFG.service.backdate_limit_days
    for earliest in (world.days[262 - limit], world.days[263]):  # 回溯上限当天、以及默认的次日
        r = _create_with_start(invoke, "--start", str(earliest))
        assert r.exit_code == 0 and str(earliest) in r.stdout
        assert _start_of(db) == earliest
        user = db.scalars(select(User).where(User.email == EMAIL)).one()
        sub = repo.get_open_subscription(db, user.id)
        assert sub is not None
        accounts.close_subscription(db, user.id, sub.id)
        db.commit()


def test_sub_create_rejects_bad_start_days(
    world: World, db: Session, invoke: Invoke, set_now: SetNow
) -> None:
    set_now(world.days[262])
    limit = CFG.service.backdate_limit_days
    saturday = world.days[262] + timedelta(days=3)
    cases = {
        str(saturday): "不是交易日",
        str(world.days[262 - limit - 1]): "太早",
        str(world.days[264]): "不能晚于",
        "2022-13-40": "日期",
    }
    first = True
    for value, message in cases.items():
        if first:
            invoke("admin", "user", "add", "--email", EMAIL)
            first = False
        r = invoke("admin", "sub", "create", "--email", EMAIL, "--cash", "1000", "--start", value)
        assert r.exit_code == 1 and message in r.stdout, (value, r.stdout)
        assert "Traceback" not in r.stdout
    db.rollback()
    assert db.scalars(select(Subscription)).all() == []
