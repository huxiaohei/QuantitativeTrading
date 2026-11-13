"""端到端测试：在真实策略会交易的数据集上跑命令行，以及未来函数（截断不变性）检验。"""

import re
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from markupsafe import escape
from typer.testing import CliRunner

from qt.backtest.commands import backtest_app
from qt.backtest.report import EXIT_LABELS, limitation_statements, run_period
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.data.update import load_calendar
from qt.features import build_panel
from tests.fakes import default_app_config, populate_trading_store

ROOT = Path(__file__).resolve().parent.parent.parent
GAPS = ROOT / "docs" / "data-gaps.md"


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


def _truncated_copy(store: ParquetStore, root: Path, cutoff: date) -> ParquetStore:
    """把 store 复制一份，删掉 cutoff 之后才出现的信息。"""
    stamp = pd.Timestamp(cutoff)
    out = ParquetStore(root)
    out.write(Table.TRADE_DAYS, store.read(Table.TRADE_DAYS))
    secs = store.read(Table.SECURITIES)
    secs["delist_date"] = secs["delist_date"].where(secs["delist_date"] <= stamp)
    out.write(Table.SECURITIES, secs)
    for table in (Table.INDUSTRIES, Table.ST_HISTORY):
        out.write(table, store.read(table))
    bench = store.read(Table.BENCHMARKS)
    out.write(Table.BENCHMARKS, bench[bench["day"] <= stamp])
    for symbol in secs["symbol"]:
        sym = str(symbol)
        bars = store.read(Table.BARS, symbols=[sym])
        out.write(Table.BARS, bars[bars["day"] <= stamp], sym)
        for table in (Table.DIVIDENDS, Table.FINANCIALS):
            frame = store.read(table, symbols=[sym])
            out.write(table, frame[frame["announce_date"] <= stamp], sym)
    return out


def test_results_up_to_a_day_do_not_depend_on_later_data(tmp_path: Path) -> None:
    full_store = ParquetStore(tmp_path / "full")
    days = populate_trading_store(full_store)
    cutoff = days[340]  # 2021 财年分红已公告（4 月 20 日）、尚未除权（6 月 15 日）
    cut_store = _truncated_copy(full_store, tmp_path / "cut", cutoff)
    cfg = _cfg(days)

    full = run_period(cfg, full_store, days[0], days[-1])
    cut = run_period(cfg, cut_store, days[0], cutoff)
    assert cut.equity.index[-1] == cutoff
    assert any(t.exit_day <= cutoff for t in full.trades)  # 截断点之前确有成交
    pd.testing.assert_series_equal(full.equity.loc[:cutoff], cut.equity)
    pd.testing.assert_series_equal(full.cash.loc[:cutoff], cut.cash)
    assert [t for t in full.trades if t.exit_day <= cutoff] == cut.trades

    calendar = load_calendar(full_store)
    full_panel = build_panel(full_store, calendar, cfg, days[0], days[-1])
    cut_panel = build_panel(cut_store, calendar, cfg, days[0], cutoff)
    for i in (0, 100, 249, 250, 251, 300, 330, 339, 340):
        pd.testing.assert_frame_equal(
            full_panel.snapshot(days[i]), cut_panel.snapshot(days[i]), check_exact=True
        )
