"""命令行入口：qt data update。"""

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from qt.config import AppConfig, load_config
from qt.data.akshare_source import AkshareSource
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from qt.data.update import (
    SharedTableError,
    check_integrity,
    count_no_bars,
    latest_trading_day,
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


@data_app.command("update")
def update(
    config: Annotated[Path, typer.Option(help="配置文件路径")] = Path("config/default.yaml"),
    symbols: Annotated[str | None, typer.Option(help="只更新这些股票，逗号分隔")] = None,
    data_dir: Annotated[Path, typer.Option(help="数据目录")] = Path("data"),
) -> None:
    """下载或增量更新全部数据，随后检查数据完整性。"""
    cfg = load_config(config)
    store = ParquetStore(data_dir)
    today = date.today()
    wanted = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None

    def progress(done: int, total: int) -> None:
        typer.echo(f"[进度] 已处理 {done}/{total} 只股票")

    try:
        report = update_all(make_source(cfg), store, cfg, today, wanted, progress)
    except SharedTableError as exc:
        typer.echo(f"共享表更新失败：{exc}")
        raise typer.Exit(code=1) from exc

    failures = data_dir / "update-failures.json"
    failures.parent.mkdir(parents=True, exist_ok=True)
    failures.write_text(
        json.dumps(dict(report.failed), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    typer.echo(
        f"更新完成：更新 {report.updated} 只，跳过 {report.skipped} 只，"
        f"失败 {len(report.failed)} 只（其中已退市且无日线 {count_no_bars(report.failed)} 只）；"
        f"失败明细见 {failures}"
    )

    calendar = load_calendar(store)
    day = latest_trading_day(calendar, today)
    problems = check_integrity(store, calendar, day)
    if problems:
        typer.echo(f"数据完整性检查发现 {len(problems)} 个问题（检查日 {day}）：")
        for line in problems[:_MAX_PRINTED_PROBLEMS]:
            typer.echo(f"  - {line}")
        if len(problems) > _MAX_PRINTED_PROBLEMS:
            typer.echo(f"  ……其余 {len(problems) - _MAX_PRINTED_PROBLEMS} 条略")
        raise typer.Exit(code=1)
    typer.echo(f"数据完整性检查通过（检查日 {day}）")
