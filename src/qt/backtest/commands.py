"""命令行：qt backtest run 与 qt backtest sweep。"""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
import yaml

from qt.backtest.metrics import compute_metrics
from qt.backtest.report import (
    BacktestInputError,
    last_bar_day,
    load_benchmarks,
    load_last_closes,
    render_report,
    resolve_period,
    run_period,
)
from qt.backtest.sweep import parse_param, run_sweep
from qt.config import load_config
from qt.data.store import ParquetStore
from qt.data.update import DataIntegrityError

backtest_app = typer.Typer(help="回测与报告", no_args_is_help=True)


class Period(StrEnum):
    """回测区间：样本内、样本外、全区间。"""

    IN = "in"
    OUT = "out"
    FULL = "full"


@backtest_app.callback()
def _root() -> None:
    """回测与报告。"""


@backtest_app.command("run")
def run(
    period: Annotated[
        Period, typer.Option(help="区间：in 样本内、out 样本外、full 全部")
    ] = Period.IN,
    config: Annotated[Path, typer.Option(help="配置文件路径")] = Path("config/default.yaml"),
    data_dir: Annotated[Path, typer.Option(help="数据目录")] = Path("data"),
    out: Annotated[Path, typer.Option(help="报告输出目录")] = Path("reports"),
    data_gaps: Annotated[Path, typer.Option(help="数据局限文档")] = Path("docs/data-gaps.md"),
) -> None:
    """运行回测并生成单文件 HTML 报告。"""
    try:
        cfg = load_config(config)
    except (OSError, yaml.YAMLError, ValueError) as exc:  # ValidationError 也是 ValueError
        typer.echo(f"无法读取配置文件 {config}：{exc}")
        raise typer.Exit(code=1) from exc
    if not data_gaps.is_file():
        typer.echo(f"找不到数据局限文档 {data_gaps}，请用 --data-gaps 指定")
        raise typer.Exit(code=1)
    store = ParquetStore(data_dir)
    last = last_bar_day(store)  # 只查一次，传给下面两步
    if last is None:
        typer.echo("无法回测：存储中没有日线数据，请先运行 qt data update")
        raise typer.Exit(code=1)
    try:
        start, end = resolve_period(cfg, store, period.value, last_day=last)
        result = run_period(cfg, store, start, end, last_day=last)
    except (BacktestInputError, DataIntegrityError) as exc:
        typer.echo(f"无法回测：{exc}")
        raise typer.Exit(code=1) from exc

    benchmarks = load_benchmarks(store, cfg, result.equity.index)
    metrics = compute_metrics(result.equity, result.trades)
    held = [p.symbol for p in result.open_positions]
    closes = load_last_closes(store, held, result.equity.index[-1])
    name = f"backtest-{period.value}-{datetime.now():%Y%m%d-%H%M%S}.html"
    path = render_report(
        result,
        metrics,
        benchmarks.series,
        cfg,
        period.value,
        data_gaps,
        out / name,
        last_closes=closes,
    )
    typer.echo(f"报告已生成：{path}")
    typer.echo(
        f"区间 {result.equity.index[0]} 至 {result.equity.index[-1]}；"
        f"回测全区间：年化收益 {metrics.annual_return:.2%}，最大回撤 {metrics.max_drawdown:.2%}，"
        f"夏普 {metrics.sharpe:.2f}，交易 {metrics.trade_count} 笔，胜率 {metrics.win_rate:.2%}"
    )
    for code in benchmarks.missing:
        typer.echo(f"提示：基准 {code} 缺少数据，已从报告中省略")


SWEEP_LABELS = {
    "annual_return": "年化收益",
    "max_drawdown": "最大回撤",
    "sharpe": "夏普",
    "win_rate": "胜率",
    "trade_count": "交易笔数",
}


def _format_table(table: pd.DataFrame) -> str:
    """把扫描结果排成文本表：收益类显示为百分比，列名换成中文，参数列保留完整路径。"""
    shown = table.copy()
    for col in ("annual_return", "max_drawdown", "win_rate"):
        shown[col] = [f"{v:.2%}" for v in table[col]]
    shown["sharpe"] = [f"{v:.2f}" for v in table["sharpe"]]
    return shown.rename(columns=SWEEP_LABELS).to_string(index=False)


@backtest_app.command("sweep")
def sweep(
    param: Annotated[
        list[str] | None,
        typer.Option(help="要对比的参数，如 strategy.min_dividend_yield=0.03,0.04；可重复"),
    ] = None,
    config: Annotated[Path, typer.Option(help="配置文件路径")] = Path("config/default.yaml"),
    data_dir: Annotated[Path, typer.Option(help="数据目录")] = Path("data"),
    out: Annotated[Path, typer.Option(help="结果输出目录")] = Path("reports"),
) -> None:
    """对参数组合逐一回测并对比。只用样本内区间，样本外数据不参与调参。"""
    try:
        cfg = load_config(config)
    except (OSError, yaml.YAMLError, ValueError) as exc:  # ValidationError 也是 ValueError
        typer.echo(f"无法读取配置文件 {config}：{exc}")
        raise typer.Exit(code=1) from exc
    if not param:
        typer.echo("请至少用 --param 指定一个参数，如 --param strategy.stop_loss=0.10,0.15")
        raise typer.Exit(code=1)
    grid: dict[str, list[object]] = {}
    try:
        for text in param:
            path, values = parse_param(cfg, text)
            if path in grid:
                raise ValueError(f"参数 {path} 重复指定，请把取值合并到同一个 --param 里")
            grid[path] = values
    except ValueError as exc:
        typer.echo(f"参数错误：{exc}")
        raise typer.Exit(code=1) from exc

    total = 1
    for values in grid.values():
        total *= len(values)
    typer.echo(
        f"共 {total} 组参数，只在样本内区间 {cfg.backtest.in_sample_start} 至 "
        f"{cfg.backtest.in_sample_end} 回测"
    )

    def progress(done: int, count: int) -> None:
        typer.echo(f"[进度] 已完成 {done}/{count} 组")

    store = ParquetStore(data_dir)
    try:
        table = run_sweep(cfg, store, grid, progress)
    except (ValueError, DataIntegrityError) as exc:  # BacktestInputError 也是 ValueError
        typer.echo(f"无法回测：{exc}")
        raise typer.Exit(code=1) from exc

    out.mkdir(parents=True, exist_ok=True)
    path = out / f"sweep-{datetime.now():%Y%m%d-%H%M%S}.csv"
    table.to_csv(path, index=False, encoding="utf-8")
    typer.echo(_format_table(table))
    typer.echo(f"对比结果已保存：{path}")
