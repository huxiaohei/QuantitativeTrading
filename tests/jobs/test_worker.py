"""定时进程：何时到点、休眠唤醒后立即补跑、数据失败重试与超时告警；以及两个命令。"""

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from qt import cli
from qt.db.models import RerunRequest
from qt.jobs import worker
from qt.jobs.daily import (
    RUN_LOCK_KEY,
    DailyRunBusy,
    RunContext,
    catch_up,
    run_day,
    run_deadline_at,
)
from qt.jobs.worker import is_due, run_forever
from qt.service import reruns
from qt.service.accounts import create_user
from qt.service.actor import CLI
from tests.jobs.conftest import (
    ADMIN,
    DAY,
    BrokenSource,
    Env,
    daily_run,
    evening,
    mirror_source,
    processed_through,
    subscribe,
)


class _Stop(BaseException):
    """测试用：让假的 sleep 结束 run_forever 的循环。"""


def _ticking(env: Env, until: datetime, seen: list[float]) -> Callable[[float], None]:
    """每次 sleep 把假时钟拨快相应秒数，超过 until 就结束循环。"""

    def sleep(seconds: float) -> None:
        seen.append(seconds)
        env.clock.now += timedelta(seconds=seconds)
        if env.clock.now > until:
            raise _Stop

    return sleep


def test_is_due_and_deadline() -> None:
    sh = ZoneInfo("Asia/Shanghai")
    assert not is_due(datetime(2026, 11, 16, 17, 59, tzinfo=sh), "18:00", None)
    assert is_due(datetime(2026, 11, 16, 18, 0, tzinfo=sh), "18:00", None)
    assert not is_due(
        datetime(2026, 11, 16, 18, 5, tzinfo=sh), "18:00", datetime(2026, 11, 16, 18, 0, tzinfo=sh)
    )
    # 休眠跨过了周二 18:00：周三上午醒来立即到点
    assert is_due(
        datetime(2026, 11, 18, 9, 0, tzinfo=sh), "18:00", datetime(2026, 11, 16, 17, 0, tzinfo=sh)
    )

    def weekday(d: date) -> bool:
        return d.weekday() < 5

    # 周一上午：最近的运行时刻是周五 18:00（周末不是交易日），周五 18:30 已尝试过
    friday = datetime(2026, 11, 13, 18, 30, tzinfo=sh)
    assert not is_due(datetime(2026, 11, 16, 9, 0, tzinfo=sh), "18:00", friday, weekday)
    assert not is_due(datetime(2026, 11, 14, 18, 0, tzinfo=sh), "18:00", None, weekday)  # 周六
    assert is_due(datetime(2026, 11, 16, 18, 0, tzinfo=sh), "18:00", friday, weekday)


def test_worker_alerts_once_when_deadline_passes_without_success(env: Env, db: Session) -> None:
    days = env.days
    sub = subscribe(db, "a@example.com", days[DAY])
    source = mirror_source(env.store, BrokenSource)  # 数据一直失败
    env.source = source
    env.clock.now = evening(days[DAY], 18, 30)
    seen: list[float] = []
    until = evening(days[DAY + 1], 10, 0)  # 跨过次日 08:30
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, until, seen))
    assert set(seen) == {60}
    subjects = [m.subject for m in env.sender.sent]
    assert subjects.count("【系统异常】每日运行超时") == 1
    assert subjects.count("【系统异常】数据更新失败") == 1  # 每天同一标题只告警一次
    assert all(m.to_addr == ADMIN for m in env.sender.sent)
    assert processed_through(db, sub, "system") is None
    run = daily_run(db, days[DAY])
    assert run is not None and run.status == "data_failed"
    # 每 30 分钟重试一次，直到次日 08:30：18:30 启动时 1 次，19:00 至 08:00 共 27 次
    assert len([c for c in source.calls if c[0] == "securities"]) == 28


def test_worker_runs_catch_up_immediately_after_wake_from_sleep(env: Env, db: Session) -> None:
    days = env.days
    monday, tuesday, wednesday = days[DAY], days[DAY + 1], days[DAY + 2]
    sub = subscribe(db, "a@example.com", monday)
    env.clock.now = evening(monday, 17, 0)
    calls = 0

    def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            env.clock.now = evening(wednesday, 9, 0)  # 电脑从周一 17:00 一直睡到周三 09:00
            return
        raise _Stop

    with pytest.raises(_Stop):
        run_forever(env.ctx, sleep)
    for day in (monday, tuesday):
        run = daily_run(db, day)
        assert run is not None and run.status == "done"
    assert daily_run(db, wednesday) is None
    assert processed_through(db, sub, "manual") == tuesday
    # 只有最新一天（周二）的信号发邮件；周一已过期限才补跑，告知管理员
    user_mails = [m.subject.split(" ")[0] for m in env.sender.sent if m.to_addr != ADMIN]
    assert user_mails == [f"【买入信号】{tuesday}"]
    [note] = [m for m in env.sender.sent if m.to_addr == ADMIN]
    assert note.subject == "【系统异常】补跑了已超时的交易日" and str(monday) in note.text


def test_worker_keeps_retrying_tonight_after_backlog_fails(env: Env, db: Session) -> None:
    days = env.days
    subscribe(db, "a@example.com", days[DAY])  # 周一起积压到周五
    source = mirror_source(env.store, BrokenSource)
    env.source = source
    friday = days[DAY + 4]
    monday = days[DAY + 5]
    assert (friday.weekday(), monday.weekday()) == (4, 0)
    env.clock.now = evening(friday, 20, 0)
    saturday = friday + timedelta(days=1)
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, evening(saturday, 10, 0), []))
    # 期限是下一个交易日（周一）08:30，周六上午仍在重试：20:00 启动 1 次，之后每 30 分钟一次
    attempts = len([c for c in source.calls if c[0] == "securities"])
    assert attempts == 1 + 28
    timeouts = [m.text for m in env.sender.sent if m.subject == "【系统异常】每日运行超时"]
    assert len([t for t in timeouts if f"交易日 {days[DAY + 3]} " in t]) == 1  # 周四的已过期
    assert not [t for t in timeouts if f"交易日 {friday} " in t]  # 周五的还没到期
    expected = datetime.combine(monday, time(8, 30), tzinfo=ZoneInfo("Asia/Shanghai"))
    assert run_deadline_at(friday, "08:30", env.calendar) == expected


def test_deadline_alert_is_retried_when_queueing_it_fails(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = env.days
    subscribe(db, "a@example.com", days[DAY])
    env.source = mirror_source(env.store, BrokenSource)
    real_alert = worker.alert
    failed_once: list[str] = []

    def flaky_alert(ctx: RunContext, day: date, title: str, lines: Sequence[str]) -> None:
        if title == "每日运行超时" and not failed_once:
            failed_once.append(title)
            raise RuntimeError("模拟数据库暂时不可用")
        real_alert(ctx, day, title, lines)

    monkeypatch.setattr(worker, "alert", flaky_alert)
    env.clock.now = evening(days[DAY], 18, 30)
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, evening(days[DAY + 1], 10, 0), []))
    assert failed_once  # 第一次入队失败
    subjects = [m.subject for m in env.sender.sent]
    assert subjects.count("【系统异常】每日运行超时") == 1  # 下一个节拍补上，仍只一封


def test_worker_survives_unexpected_error_and_alerts(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscribe(db, "a@example.com", env.days[DAY])

    def boom(ctx: RunContext) -> list[tuple[date, str]]:
        raise RuntimeError("模拟意外错误")

    monkeypatch.setattr("qt.jobs.worker.catch_up", boom)
    seen: list[float] = []
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, env.clock.now + timedelta(hours=2), seen))
    assert [m.subject for m in env.sender.sent] == ["【系统异常】每日运行出错"]
    assert "模拟意外错误" in env.sender.sent[0].text


def test_failed_alert_does_not_cancel_retry(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscribe(db, "a@example.com", env.days[DAY])
    calls: list[datetime] = []

    def boom(ctx: RunContext) -> list[tuple[date, str]]:
        calls.append(ctx.now())
        raise RuntimeError("模拟意外错误")

    real_alert = worker.alert
    alerts = 0

    def flaky_alert(ctx: RunContext, day: date, title: str, lines: Sequence[str]) -> None:
        nonlocal alerts
        alerts += 1
        if alerts == 1:
            raise RuntimeError("模拟数据库暂时不可用")
        real_alert(ctx, day, title, lines)

    monkeypatch.setattr(worker, "catch_up", boom)
    monkeypatch.setattr(worker, "alert", flaky_alert)
    start = env.clock.now
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, start + timedelta(hours=2), []))
    assert calls == [start + timedelta(minutes=30 * i) for i in range(5)]
    assert [m.subject for m in env.sender.sent] == ["【系统异常】每日运行出错"]


def test_failing_first_attempt_does_not_end_run_forever(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: object, now: datetime) -> None:
        raise RuntimeError("模拟启动时出错")

    monkeypatch.setattr("qt.jobs.worker._Worker.attempt", broken)
    seen: list[float] = []
    with pytest.raises(_Stop):
        run_forever(env.ctx, _ticking(env, env.clock.now + timedelta(minutes=3), seen))
    assert seen == [60, 60, 60, 60]


def test_run_day_command(env: Env, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    subscribe(db, "a@example.com", env.days[DAY])
    ctx = env.ctx

    def make_context(config: Path, data_dir: Path) -> RunContext:
        return ctx

    monkeypatch.setattr(cli, "make_context", make_context)
    result = CliRunner().invoke(cli.app, ["run-day", "--day", "2022-01-03", "--no-update"])
    assert result.exit_code == 0, result.output
    assert "2022-01-03" in result.output and "done" in result.output
    assert [m.to_addr for m in env.sender.sent] == ["a@example.com"]
    bad = CliRunner().invoke(cli.app, ["run-day", "--day", "2022-01-01", "--no-update"])
    assert bad.exit_code == 1 and "不是交易日" in bad.output


def test_make_context_requires_admin_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    config = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        cli.make_context(config, tmp_path)
    monkeypatch.setenv("ADMIN_EMAIL", "Boss@Example.com")
    ctx = cli.make_context(config, tmp_path)
    assert ctx.admin_email == "boss@example.com"
    assert ctx.now().tzinfo is not None


# ---------- 同一时间只有一个每日运行 ----------


@contextmanager
def _another_run(env: Env) -> Generator[None]:
    """模拟另一个进程（qt run-day 或 qt worker）正在运行：占住每日运行的咨询锁。"""
    with env.engine.connect() as conn:
        assert conn.execute(select(func.pg_try_advisory_lock(RUN_LOCK_KEY))).scalar_one()
        try:
            yield
        finally:
            conn.execute(select(func.pg_advisory_unlock(RUN_LOCK_KEY)))


def test_second_daily_run_exits_without_running_or_alerting(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = env.days[DAY]
    sub = subscribe(db, "a@example.com", day)
    ctx = env.ctx
    with _another_run(env):
        with pytest.raises(DailyRunBusy, match="另一个每日运行正在进行"):
            run_day(ctx, day, True, False)
        with pytest.raises(DailyRunBusy):
            catch_up(ctx)

        def make_context(config: Path, data_dir: Path) -> RunContext:
            return ctx

        monkeypatch.setattr(cli, "make_context", make_context)
        r = CliRunner().invoke(cli.app, ["run-day", "--day", str(day), "--no-update"])
        assert r.exit_code == 1 and "另一个每日运行正在进行" in r.output
        assert "Traceback" not in r.output
        with pytest.raises(_Stop):  # worker：启动时补跑拿不到锁，只记日志，不告警
            run_forever(ctx, _ticking(env, env.clock.now + timedelta(minutes=2), []))
    assert daily_run(db, day) is None and env.sender.sent == []
    assert processed_through(db, sub, "manual") is None
    assert run_day(ctx, day, True, False) == "done"  # 锁释放之后照常运行


def _request_rerun(db: Session, env: Env) -> int:
    admin = create_user(db, "boss@example.com", is_admin=True, actor=CLI)
    row = reruns.request(db, admin.id, env.clock.now)
    db.commit()
    return row.id


def _one_tick(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[RunContext],
    result: Exception | None,
) -> None:
    """启动补跑之后再跑一个空转节拍；catch_up 换成记录调用的替身（result 不为空时抛出）。

    启动时的补跑算一次调用；节拍里常规补跑不会触发（刚尝试过、没有重试计划），
    所以多出来的调用就是处理重试请求。
    """

    def fake_catch_up(ctx: RunContext) -> list[tuple[date, str]]:
        calls.append(ctx)
        if result is not None and len(calls) > 1:
            raise result
        return [(env.days[DAY], "done")]

    monkeypatch.setattr(worker, "catch_up", fake_catch_up)
    ticks = 0

    def sleep(seconds: float) -> None:
        nonlocal ticks
        del seconds
        ticks += 1
        if ticks > 1:
            raise _Stop

    with pytest.raises(_Stop):
        run_forever(env.ctx, sleep)


def test_worker_handles_rerun_request_in_one_tick(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = _request_rerun(db, env)
    calls: list[RunContext] = []
    _one_tick(env, monkeypatch, calls, None)
    assert len(calls) == 2  # 启动 1 次 + 重试请求 1 次
    db.rollback()
    row = db.get(RerunRequest, request_id)
    assert row is not None
    assert row.handled_at == env.clock.now
    assert row.result == f"{env.days[DAY]} done"


def test_worker_finishes_rerun_request_when_catch_up_raises(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = _request_rerun(db, env)
    calls: list[RunContext] = []
    _one_tick(env, monkeypatch, calls, ValueError("数据坏了"))
    assert len(calls) == 2
    db.rollback()
    row = db.get(RerunRequest, request_id)
    assert row is not None and row.handled_at is not None
    assert row.result is not None and "ValueError" in row.result and "数据坏了" in row.result


def test_worker_does_not_catch_up_without_rerun_request(
    env: Env, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    del db
    calls: list[RunContext] = []
    _one_tick(env, monkeypatch, calls, None)
    assert len(calls) == 1  # 只有启动时那一次


def test_make_context_reads_base_url_once_and_tolerates_a_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("ADMIN_EMAIL", "boss@example.com")
    config = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    monkeypatch.delenv("QT_BASE_URL", raising=False)
    assert cli.make_context(config, tmp_path).base_url is None
    monkeypatch.setenv("QT_BASE_URL", "https://quant.test/")
    assert cli.make_context(config, tmp_path).base_url == "https://quant.test"
    monkeypatch.setenv("QT_BASE_URL", "ftp://坏地址")
    assert cli.make_context(config, tmp_path).base_url is None  # 写错不让常驻进程起不来
