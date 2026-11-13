"""端到端测试：在真实策略会交易的数据集上跑命令行（未来函数检验见 test_lookahead.py）。"""

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import yaml
from markupsafe import escape
from typer.testing import CliRunner

from qt.backtest.commands import backtest_app
from qt.backtest.report import EXIT_LABELS, limitation_statements, run_period
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.models import ExitReason, Trade
from tests.fakes import default_app_config, make_dividends, populate_trading_store

ROOT = Path(__file__).resolve().parent.parent.parent
GAPS = ROOT / "docs" / "data-gaps.md"
CUT_SYMBOL = "600003"


def _cfg(days: list[date]) -> AppConfig:
    cfg = default_app_config()
    backtest = cfg.backtest.model_copy(
        update={
            "in_sample_start": days[0],
            "in_sample_end": days[360],
            "out_sample_start": days[361],
        }
    )
    return cfg.model_copy(update={"backtest": backtest})


def test_run_command_on_trading_store_renders_every_section(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    days = populate_trading_store(store)
    cfg = _cfg(days)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    out_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        backtest_app,
        [
            "run",
            "--period",
            "full",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--out",
            str(out_dir),
            "--data-gaps",
            str(GAPS),
        ],
    )
    assert result.exit_code == 0, result.output
    (written,) = out_dir.glob("backtest-full-*.html")
    html = written.read_text(encoding="utf-8")

    expected = run_period(cfg, store, cfg.backtest.in_sample_start, days[-1])
    assert expected.trades and expected.open_positions  # 数据集确实有成交与期末持仓
    trades = html[html.index("<h2>6.") : html.index("期末持仓")]
    rows = re.findall(r"<tr><td class=\"l\">(\d{6})</td><td>([\d-]+)</td><td>([\d-]+)</td>", trades)
    assert rows == [(t.symbol, str(t.entry_day), str(t.exit_day)) for t in expected.trades]
    for reason in {t.exit_reason for t in expected.trades}:
        assert EXIT_LABELS[reason.value] in trades
    held = html[html.index("期末持仓") : html.index("<h2>7.")]
    for pos in expected.open_positions:
        assert f'<td class="l">{pos.symbol}</td>' in held
    limits = html[html.index("<h2>8.") :]
    assert "幸存者偏差相关统计" in limits
    assert "历史行情尚未做完整性扫描（运行 qt data check）" in limits
    for text in limitation_statements(cfg):
        assert f"<li>{escape(text)}</li>" in limits


def _cut_store(root: Path) -> list[date]:
    """交易数据集，但 600003 的 FY2020 分红从 0.6 降到 0.5，且 FY2021 尚未公告。

    第 250 个交易日前后（2021 年 12 月）Y*=2020，当前分红水平 0.5 低于上一年度的 0.6，
    dividend_cut 已为真；股息率 0.5/10 = 5% 仍满足买入条件。
    """
    store = ParquetStore(root)
    days = populate_trading_store(store)
    cash = {fy: 0.6 for fy in range(2015, 2020)} | {2020: 0.5}
    rows = [(fy, date(fy + 1, 4, 20), date(fy + 1, 6, 15), c) for fy, c in cash.items()]
    store.write(Table.DIVIDENDS, make_dividends(CUT_SYMBOL, rows), symbol=CUT_SYMBOL)
    return days


def _quick_fundamental_exits(trades: Sequence[Trade], days: list[date]) -> list[Trade]:
    """买入次一交易日即因基本面卖出的交易。"""
    return [
        t
        for t in trades
        if t.exit_reason is ExitReason.FUNDAMENTAL
        and days.index(t.exit_day) == days.index(t.entry_day) + 1
    ]


def test_pre_existing_dividend_cut_is_not_bought_when_excluded(tmp_path: Path) -> None:
    days = _cut_store(tmp_path / "data")
    store = ParquetStore(tmp_path / "data")
    base = _cfg(days)

    def run(exclude: bool) -> list[Trade]:
        strategy = base.strategy.model_copy(update={"exclude_dividend_cut": exclude})
        cfg = base.model_copy(update={"strategy": strategy})
        return run_period(cfg, store, days[0], days[360]).trades

    # 开关关闭：600003 买入后次日即被基本面止损卖出（数据集确实会触发缺陷）
    off = run(exclude=False)
    assert CUT_SYMBOL in {t.symbol for t in _quick_fundamental_exits(off, days)}
    # 开关打开：不再买入已削减分红的股票
    on = run(exclude=True)
    assert _quick_fundamental_exits(on, days) == []
    assert CUT_SYMBOL not in {t.symbol for t in on}
