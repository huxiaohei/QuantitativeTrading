"""数据库备份：调用 pg_dump 导出纯 SQL，压缩后写到备份目录，并清理超过保留天数的旧备份。

备份文件包含全部使用者的数据，所以：文件权限 0600、目录权限 0700；数据库密码只通过环境变量
PGPASSWORD 传给 pg_dump，不出现在命令行参数和日志里；先写临时文件，成功后再改名，失败不留半个文件。
"""

import gzip
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

from qt.db.engine import database_url

_log = logging.getLogger(__name__)

BACKUP_DIR_ENV = "QT_BACKUP_DIR"
DEFAULT_BACKUP_DIR = "backups"
KEEP_DAYS = 14
_NAME = re.compile(r"^qt-(\d{4})(\d{2})(\d{2})\.sql\.gz$")
_CHUNK = 1 << 20
_MAX_STDERR = 500


class BackupError(RuntimeError):
    """备份失败；消息里不含数据库密码。"""


def backup_name(day: date) -> str:
    return f"qt-{day:%Y%m%d}.sql.gz"


def _dump_command(pg_dump: str, url: str) -> tuple[list[str], dict[str, str]]:
    """pg_dump 的命令行与环境；密码只放在环境里。"""
    parsed = make_url(url)
    if not parsed.host or not parsed.database or not parsed.username:
        raise BackupError("DATABASE_URL 缺少主机、库名或用户名，无法备份")
    command = [pg_dump, "-h", parsed.host, "-U", parsed.username, "-d", parsed.database]
    if parsed.port is not None:
        command += ["-p", str(parsed.port)]
    command += ["--no-owner", "--no-privileges"]
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return command, env


def _write_dump(command: list[str], env: dict[str, str], tmp: Path) -> None:
    """运行 pg_dump，标准输出边读边压缩写入 tmp（创建时即为 0600）。"""
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with tempfile.TemporaryFile() as err, os.fdopen(fd, "wb") as raw:
        try:
            proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=err)
        except OSError as exc:
            raise BackupError(f"无法运行 {command[0]}：{exc.strerror or exc}") from exc
        with proc:
            assert proc.stdout is not None
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
                shutil.copyfileobj(proc.stdout, out, _CHUNK)
        if proc.returncode != 0:
            err.seek(0)
            detail = err.read().decode("utf-8", errors="replace").strip()[-_MAX_STDERR:]
            raise BackupError(f"pg_dump 退出码 {proc.returncode}：{detail}")


def prune_backups(directory: Path, today: date, keep_days: int = KEEP_DAYS) -> list[Path]:
    """删除文件名日期早于 today - keep_days 的备份，返回被删除的文件；其他文件不动。"""
    cutoff = today - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        match = _NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        try:
            stamp = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        if stamp < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def backup_database(
    url: str,
    directory: Path,
    today: date,
    keep_days: int = KEEP_DAYS,
    pg_dump: str = "pg_dump",
) -> Path:
    """备份到 directory/qt-YYYYMMDD.sql.gz（同一天重复备份会覆盖），成功后清理旧备份。"""
    command, env = _dump_command(pg_dump, url)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    final = directory / backup_name(today)
    tmp = directory / f".{final.name}.tmp"
    tmp.unlink(missing_ok=True)
    try:
        _write_dump(command, env, tmp)
        tmp.chmod(0o600)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    removed = prune_backups(directory, today, keep_days)
    _log.info("数据库已备份到 %s，清理旧备份 %d 个", final, len(removed))
    return final


def backup_from_env(today: date) -> Path:
    """按环境变量 DATABASE_URL 与 QT_BACKUP_DIR（默认 backups）备份。"""
    directory = Path(os.environ.get(BACKUP_DIR_ENV, "").strip() or DEFAULT_BACKUP_DIR)
    return backup_database(database_url(), directory, today)
