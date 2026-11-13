"""端到端测试：在真实策略会交易的数据集上跑命令行（未来函数检验见 test_lookahead.py）。"""

import re
from datetime import date
from pathlib import Path

import yaml
from markupsafe import escape
from typer.testing import CliRunner

from qt.backtest.commands import backtest_app
from qt.backtest.report import EXIT_LABELS, limitation_statements, run_period
from qt.config import AppConfig
from qt.data.store import ParquetStore
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
