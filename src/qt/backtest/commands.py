"""命令行：qt backtest run。"""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from qt.backtest.metrics import compute_metrics
from qt.backtest.report import (
    BacktestInputError,
    load_benchmarks,
    load_last_closes,
    render_report,
    resolve_period,
    run_period,
)
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
    cfg = load_config(config)
    store = ParquetStore(data_dir)
    try:
        start, end = resolve_period(cfg, store, period.value)
        result = run_period(cfg, store, start, end)
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
        f"年化收益 {metrics.annual_return:.2%}，最大回撤 {metrics.max_drawdown:.2%}，"
        f"夏普 {metrics.sharpe:.2f}，交易 {metrics.trade_count} 笔，胜率 {metrics.win_rate:.2%}"
    )
    for code in benchmarks.missing:
        typer.echo(f"提示：基准 {code} 缺少数据，已从报告中省略")
