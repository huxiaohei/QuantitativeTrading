"""旧版本写入的数据文件：命令行给出中文提示并以 1 退出，不打印 traceback。"""

from pathlib import Path

import pandas as pd
import yaml
from typer.testing import CliRunner, Result

from qt.backtest.commands import backtest_app
from qt.data.schema import Table
from qt.data.store import ParquetStore
from tests.fakes import default_app_config, populate_trading_store

GAPS = Path(__file__).resolve().parent.parent.parent / "docs" / "data-gaps.md"


def _old_schema_store(tmp_path: Path) -> Path:
    """真实会成交的数据集，其中一只股票的分红文件换成缺少 bonus_per_share 的旧格式。"""
    root = tmp_path / "data"
    store = ParquetStore(root)
    days = populate_trading_store(store)
    old = store.read(Table.DIVIDENDS, symbols=["600002"]).drop(columns=["bonus_per_share"])
    old.to_parquet(root / "dividends" / "600002.parquet", index=False)
    cfg = default_app_config()
    backtest = cfg.backtest.model_copy(
        update={
            "in_sample_start": days[0],
            "in_sample_end": days[360],
            "out_sample_start": days[361],
        }
    )
    raw = cfg.model_copy(update={"backtest": backtest}).model_dump(mode="json")
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return root


def _check(result: Result) -> None:
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "dividends" in result.output
    assert "bonus_per_share" in result.output
    assert "删除该目录后重新运行 qt data update" in result.output


def test_run_command_reports_old_schema_file(tmp_path: Path) -> None:
    root = _old_schema_store(tmp_path)
    args = ["run", "--period", "full", "--config", str(tmp_path / "cfg.yaml")]
    args += ["--data-dir", str(root), "--out", str(tmp_path / "r"), "--data-gaps", str(GAPS)]
    _check(CliRunner().invoke(backtest_app, args))


def test_sweep_command_reports_old_schema_file(tmp_path: Path) -> None:
    root = _old_schema_store(tmp_path)
    args = ["sweep", "--param", "strategy.stop_loss=0.1,0.15"]
    args += ["--config", str(tmp_path / "cfg.yaml"), "--data-dir", str(root)]
    args += ["--out", str(tmp_path / "r")]
    _check(CliRunner().invoke(backtest_app, args))
    assert not list(tmp_path.glob("r/*.csv"))


def test_old_schema_frame_is_really_missing_the_column(tmp_path: Path) -> None:
    root = _old_schema_store(tmp_path)
    columns = pd.read_parquet(root / "dividends" / "600002.parquet").columns
    assert "bonus_per_share" not in columns
