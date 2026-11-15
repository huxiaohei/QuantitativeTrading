"""数据库备份：权限、密码不上命令行、失败不留文件、保留天数；以及定时进程里的备份与告警。"""

import gzip
import stat
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.data.schema import Table
from qt.db.models import Outbox
from qt.jobs.backup import BackupError, backup_database, backup_from_env, prune_backups
from qt.jobs.worker import run_backup, run_forever
from tests.jobs.conftest import ADMIN, DAY, BrokenSource, Env, daily_run, subscribe

URL = "postgresql+psycopg://qt:s3cret-pass@db:5432/qt"
TODAY = date(2026, 11, 16)


def _fake_pg_dump(tmp_path: Path, body: str = 'echo "-- dump"', code: int = 0) -> str:
    """假的 pg_dump：把参数与 PGPASSWORD 记到文件，输出一行 SQL。"""
    script = tmp_path / "pg_dump"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{tmp_path}/args"\n'
        f'echo "$PGPASSWORD" > "{tmp_path}/pw"\n'
        f"{body}\n"
        f"exit {code}\n"
    )
    script.chmod(0o755)
    return str(script)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backup_writes_private_gzip_and_keeps_password_off_command_line(tmp_path: Path) -> None:
    out = tmp_path / "backups"
    path = backup_database(URL, out, TODAY, pg_dump=_fake_pg_dump(tmp_path))
    assert path == out / "qt-20261116.sql.gz"
    assert gzip.decompress(path.read_bytes()) == b"-- dump\n"
    assert _mode(path) == 0o600 and _mode(out) == 0o700
    args = (tmp_path / "args").read_text().split()
    assert args[:8] == ["-h", "db", "-U", "qt", "-d", "qt", "-p", "5432"]
    assert "s3cret-pass" not in " ".join(args)
    assert (tmp_path / "pw").read_text().strip() == "s3cret-pass"
    assert [p.name for p in out.iterdir()] == ["qt-20261116.sql.gz"]  # 没有临时文件


def test_failed_dump_leaves_no_file_and_keeps_yesterdays_backup(tmp_path: Path) -> None:
    out = tmp_path / "backups"
    good = backup_database(URL, out, TODAY - timedelta(days=1), pg_dump=_fake_pg_dump(tmp_path))
    broken = _fake_pg_dump(tmp_path, 'echo "partial"; echo "连接被拒绝" >&2', code=1)
    with pytest.raises(BackupError, match="连接被拒绝") as info:
        backup_database(URL, out, TODAY, pg_dump=broken)
    assert "s3cret-pass" not in str(info.value)
    assert [p.name for p in out.iterdir()] == [good.name]


def test_missing_pg_dump_and_bad_url_are_backup_errors(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="无法运行"):
        backup_database(URL, tmp_path / "b", TODAY, pg_dump=str(tmp_path / "nope"))
    with pytest.raises(BackupError, match="DATABASE_URL"):
        backup_database("postgresql+psycopg:///qt", tmp_path / "b", TODAY)


def test_prune_removes_only_backups_older_than_fourteen_days(tmp_path: Path) -> None:
    for name in (
        "qt-20261101.sql.gz",  # 15 天前：删
        "qt-20261102.sql.gz",  # 14 天前：留
        "qt-20261116.sql.gz",
        "notes.txt",
        "qt-20261301.sql.gz",  # 日期不合法：不动
    ):
        (tmp_path / name).write_text("x")
    removed = prune_backups(tmp_path, TODAY)
    assert [p.name for p in removed] == ["qt-20261101.sql.gz"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "notes.txt",
        "qt-20261102.sql.gz",
        "qt-20261116.sql.gz",
        "qt-20261301.sql.gz",
    ]


def test_backup_from_env_uses_database_url_and_backup_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("QT_BACKUP_DIR", str(tmp_path / "vol"))
    monkeypatch.setenv("PATH", f"{_dir_with(tmp_path)}:/usr/bin:/bin")
    assert backup_from_env(TODAY) == tmp_path / "vol" / "qt-20261116.sql.gz"


def _dir_with(tmp_path: Path) -> str:
    _fake_pg_dump(tmp_path)
    return str(tmp_path)


class _Stop(BaseException):
    """让假的 sleep 结束 run_forever 的循环。"""


class _Recorder:
    """假的备份函数：记录调用的日期，fail 为真时抛错。"""

    def __init__(self, fail: bool = False) -> None:
        self.days: list[date] = []
        self.fail = fail

    def __call__(self, day: date) -> Path:
        self.days.append(day)
        if self.fail:
            raise BackupError("pg_dump 退出码 1：连接被拒绝")
        return Path(f"qt-{day:%Y%m%d}.sql.gz")


def _start(env: Env, backup: _Recorder) -> None:
    """启动定时进程：启动时补跑一次，随即在第一次 sleep 时结束。"""

    def sleep(_: float) -> None:
        raise _Stop

    with pytest.raises(_Stop):
        run_forever(env.ctx, sleep, backup=backup)


def _alerts(db: Session) -> list[Outbox]:
    db.rollback()
    rows = db.scalars(select(Outbox).where(Outbox.to_addr == ADMIN).order_by(Outbox.id))
    return [r for r in rows if "数据库备份失败" in r.subject]


def test_worker_backs_up_once_after_a_finished_run(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    backup = _Recorder()
    _start(env, backup)
    assert (daily_run(db, day) or pytest.fail("没有运行记录")).status == "done"
    assert backup.days == [day]  # 备份日期为当天（上海时区）
    assert _alerts(db) == []


def test_failed_backup_alerts_admin_but_not_the_run(env: Env, db: Session) -> None:
    day = env.days[DAY]
    subscribe(db, "a@example.com", day)
    backup = _Recorder(fail=True)
    _start(env, backup)  # 备份出错，进程照常运行到下一个节拍
    assert (daily_run(db, day) or pytest.fail("没有运行记录")).status == "done"
    [mail] = _alerts(db)
    assert mail.subject == "【系统异常】数据库备份失败" and mail.status == "sent"
    assert "连接被拒绝" in mail.body_text
    assert [m.subject for m in env.sender.sent if m.to_addr != ADMIN] == [f"【买入信号】{day} 4 只"]


def test_backup_alert_is_deduplicated_per_day(env: Env, db: Session) -> None:
    ctx = env.ctx
    failing = _Recorder(fail=True)
    now = datetime.combine(env.days[DAY], datetime.min.time(), tzinfo=env.clock.now.tzinfo)
    run_backup(ctx, failing, now.replace(hour=20))
    run_backup(ctx, failing, now.replace(hour=21))
    assert len(_alerts(db)) == 1
    run_backup(ctx, failing, now.replace(hour=20) + timedelta(days=1))
    assert len(_alerts(db)) == 2


def test_no_backup_when_data_update_failed_or_nothing_to_run(env: Env, db: Session) -> None:
    backup = _Recorder()
    _start(env, backup)  # 没有订阅：没有要补的日子
    assert backup.days == []
    subscribe(db, "a@example.com", env.days[DAY])
    env.source = BrokenSource(
        trade_days=env.store.read(Table.TRADE_DAYS),
        securities=env.store.read(Table.SECURITIES),
    )
    _start(env, backup)
    assert backup.days == []
