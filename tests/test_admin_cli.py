"""管理命令：用 Typer 的 CliRunner 对测试库与假数据（临时目录）执行，不联网、不碰真实 data/。

命令通过环境变量找到数据库与数据目录（DATABASE_URL、QT_DATA_DIR、QT_CONFIG），
与每日任务相同；当前时间通过替换 qt.admin_cli.now 控制。
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Protocol
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
    Signal,
    Subscription,
    User,
)
from qt.features import build_panel
from qt.service.accounts import create_subscription, create_user
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
    def __call__(self, day: date, hour: int = 20) -> None: ...


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
    def _set(day: date, hour: int = 20) -> None:
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
