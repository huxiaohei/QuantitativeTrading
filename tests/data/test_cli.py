"""命令行测试（不联网）：数据源与当前时间都被替换。"""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from typer.testing import CliRunner

import qt.cli
from qt.config import AppConfig
from qt.data.source import DataSource
from tests.data.helpers import make_trade_days
from tests.fakes import (
    FakeSource,
    make_bars,
    make_industries,
    make_securities,
    weekdays,
)

DAYS = weekdays(date(2024, 1, 1), 10)  # 2024-01-01 至 2024-01-12（周五）
NOW = datetime(2024, 1, 12, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # 收盘后：as_of 为 1-12
LISTED = date(2000, 1, 3)


def _cli_source(bad: set[str] | None = None, n_symbols: int = 5) -> FakeSource:
    syms = [f"60000{i}" for i in range(n_symbols)]
    source = FakeSource(
        trade_days=make_trade_days(DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        bars=pd.concat([make_bars(s, DAYS, [10.0] * len(DAYS)) for s in syms]),
        industries=make_industries([(s, "制造业", False) for s in syms]),
    )
    source.fail_symbols = bad or set()
    return source


def _patch(monkeypatch: pytest.MonkeyPatch, source: DataSource) -> None:
    def fake_make_source(_cfg: AppConfig) -> DataSource:
        return source

    def fake_now() -> datetime:
        return NOW

    monkeypatch.setattr(qt.cli, "make_source", fake_make_source)
    monkeypatch.setattr(qt.cli, "_now", fake_now)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, source: DataSource, data_dir: Path, *extra: str
) -> tuple[int, str]:
    _patch(monkeypatch, source)
    result = CliRunner().invoke(qt.cli.app, ["data", "update", "--data-dir", str(data_dir), *extra])
    return result.exit_code, result.output


def test_cli_failed_symbols_alone_do_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, output = _run_cli(monkeypatch, _cli_source({"600004"}), tmp_path)
    assert code == 0, output
    assert "失败 1 只" in output
    assert "完整性检查通过" in output
    failures = json.loads((tmp_path / "update-failures.json").read_text(encoding="utf-8"))
    assert list(failures) == ["600004"]


def test_cli_integrity_problem_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, output = _run_cli(monkeypatch, _cli_source({"600003", "600004"}), tmp_path)
    assert code == 1
    assert "3/5" in output


def test_cli_industry_coverage_problem_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _cli_source()
    source = FakeSource(
        trade_days=make_trade_days(DAYS),
        securities=make_securities([(f"60000{i}", "sh_main", LISTED, None) for i in range(5)]),
        bars=pd.concat([make_bars(f"60000{i}", DAYS, [10.0] * len(DAYS)) for i in range(5)]),
    )  # 没有任何行业
    code, output = _run_cli(monkeypatch, source, tmp_path)
    assert code == 1
    assert "行业" in output


def test_cli_shared_table_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Broken(FakeSource):
        def st_history(self) -> pd.DataFrame:
            raise ConnectionError("断线")

    source = Broken(trade_days=make_trade_days(DAYS))
    code, output = _run_cli(monkeypatch, source, tmp_path)
    assert code == 1
    assert "st_history" in output


def test_cli_symbols_option_restricts_work_and_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _cli_source(n_symbols=5)
    code, output = _run_cli(monkeypatch, source, tmp_path, "--symbols", "600000,600001")
    assert code == 0, output  # 覆盖率只按指定的股票计算
    assert sorted({str(c[1][0]) for c in source.calls if c[0] == "bars"}) == ["600000", "600001"]


def test_cli_progress_line_uses_the_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, _cli_source(n_symbols=5))
    code = qt.cli.run_update(Path("config/default.yaml"), None, tmp_path, progress_every=2)
    assert code == 0
    out = capsys.readouterr().out
    assert "[进度] 日线 已处理 2/5" in out
    assert "[进度] 日线 已处理 4/5" in out
    assert "已处理 5/5" not in out


def test_cli_does_not_store_bars_after_as_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 现在是 1-12 15:30：当天还没收盘，as_of 是 1-11，数据源即使给了 1-12 的行也不能入库
    early = datetime(2024, 1, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    _patch(monkeypatch, _cli_source())
    monkeypatch.setattr(qt.cli, "_now", lambda: early)
    qt.cli.run_update(Path("config/default.yaml"), None, tmp_path)
    from qt.data.schema import Table
    from qt.data.store import ParquetStore

    bars = ParquetStore(tmp_path).read(Table.BARS)
    assert bars["day"].max() == pd.Timestamp(2024, 1, 11)
