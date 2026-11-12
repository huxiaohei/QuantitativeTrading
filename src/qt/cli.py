"""命令行入口：qt data update。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from qt.config import AppConfig, load_config
from qt.data.akshare_source import AkshareSource
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    SharedTableError,
    check_integrity,
    count_delist_missing,
    count_no_bars,
    latest_closed_day,
    load_calendar,
    update_all,
)

app = typer.Typer(help="A 股量化回测工具", no_args_is_help=True)
data_app = typer.Typer(help="数据下载与检查", no_args_is_help=True)
app.add_typer(data_app, name="data")

# 完整性问题最多逐条打印的数量
_MAX_PRINTED_PROBLEMS = 50


def make_source(cfg: AppConfig) -> DataSource:
    """创建真实数据源；测试里替换成假数据源。"""
    return AkshareSource.from_config(cfg)


def _now() -> datetime:
    """当前时间（上海时区）；测试里替换。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def run_update(
    config: Path, symbols: list[str] | None, data_dir: Path, progress_every: int = 100
) -> int:
    """执行一次更新并检查完整性，返回退出码（0 正常，1 有共享表失败或数据问题）。"""
    cfg = load_config(config)
    store = ParquetStore(data_dir)
    source = make_source(cfg)

    def progress(stage: str, done: int, total: int) -> None:
        typer.echo(f"[进度] {stage} 已处理 {done}/{total} 只股票")

    try:
        # 先刷新交易日历，据此确定 as_of（最近一个已完整收盘的交易日）
        try:
            store.write(Table.TRADE_DAYS, source.trade_days())
        except Exception as exc:
            raise SharedTableError(f"trade_days 更新失败：{type(exc).__name__}: {exc}") from exc
        as_of = latest_closed_day(_now(), load_calendar(store))
        typer.echo(f"数据截止日（最近一个已收盘交易日）：{as_of}")
        report = update_all(source, store, cfg, as_of, symbols, progress, progress_every)
    except SharedTableError as exc:
        typer.echo(f"共享表更新失败：{exc}")
        return 1

    failures = data_dir / "update-failures.json"
    failures.parent.mkdir(parents=True, exist_ok=True)
    failures.write_text(
        json.dumps(dict(report.failed), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    typer.echo(
        f"更新完成：更新 {report.updated} 只，跳过 {report.skipped} 只，"
        f"失败 {len(report.failed)} 只（其中已退市且无日线 {count_no_bars(report.failed)} 只，"
        f"退市日期缺失被丢弃 {count_delist_missing(report.failed)} 只）；失败明细见 {failures}"
    )

    problems = [*report.problems, *check_integrity(store, load_calendar(store), as_of, symbols)]
    if problems:
        typer.echo(f"数据检查发现 {len(problems)} 个问题（检查日 {as_of}）：")
        for line in problems[:_MAX_PRINTED_PROBLEMS]:
            typer.echo(f"  - {line}")
        if len(problems) > _MAX_PRINTED_PROBLEMS:
            typer.echo(f"  ……其余 {len(problems) - _MAX_PRINTED_PROBLEMS} 条略")
        return 1
    typer.echo(f"数据完整性检查通过（检查日 {as_of}）")
    return 0


@data_app.command("update")
def update(
    config: Annotated[Path, typer.Option(help="配置文件路径")] = Path("config/default.yaml"),
    symbols: Annotated[str | None, typer.Option(help="只更新这些股票，逗号分隔")] = None,
    data_dir: Annotated[Path, typer.Option(help="数据目录")] = Path("data"),
) -> None:
    """下载或增量更新全部数据，随后检查数据完整性。"""
    wanted = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    code = run_update(config, wanted, data_dir)
    if code != 0:
        raise typer.Exit(code=code)
