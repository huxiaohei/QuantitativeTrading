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
from qt.data.schema import Table
from qt.data.source import DataSource
from qt.data.store import ParquetStore
from tests.data.helpers import make_trade_days
from tests.fakes import (
    FakeSource,
    make_bars,
    make_industries,
    make_securities,
    populate_store,
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
    # 每行带墙钟时间（测试里 _now 固定为 NOW）
    assert "[进度 2024-01-12 17:00:00] 日线 已处理 2/5" in out
    assert "[进度 2024-01-12 17:00:00] 日线 已处理 4/5" in out
    assert "已处理 5/5" not in out


def test_cli_default_progress_interval_is_20(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, output = _run_cli(monkeypatch, _cli_source(n_symbols=25), tmp_path)
    assert code == 0, output
    assert "日线 已处理 20/25" in output


def test_cli_does_not_store_bars_after_as_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 现在是 1-12 15:30：当天还没收盘，as_of 是 1-11，数据源即使给了 1-12 的行也不能入库
    early = datetime(2024, 1, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    _patch(monkeypatch, _cli_source())
    monkeypatch.setattr(qt.cli, "_now", lambda: early)
    qt.cli.run_update(Path("config/default.yaml"), None, tmp_path)
    bars = ParquetStore(tmp_path).read(Table.BARS)
    assert bars["day"].max() == pd.Timestamp(2024, 1, 11)


def test_cli_fetches_trade_days_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = _cli_source()
    code, output = _run_cli(monkeypatch, source, tmp_path)
    assert code == 0, output
    assert [name for name, _ in source.calls].count("trade_days") == 1


def test_cli_data_integrity_error_is_a_chinese_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, FakeSource())  # 交易日历为空
    result = CliRunner().invoke(qt.cli.app, ["data", "update", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "trade_days 表为空" in result.output
    assert isinstance(result.exception, SystemExit)


def test_cli_network_outage_is_a_chinese_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Down(FakeSource):
        def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
            raise ConnectionError("断网")

    syms = [f"6000{i:02d}" for i in range(25)]
    source = Down(
        trade_days=make_trade_days(DAYS),
        securities=make_securities([(s, "sh_main", LISTED, None) for s in syms]),
        industries=make_industries([(s, "制造业", False) for s in syms]),
    )
    _patch(monkeypatch, source)
    result = CliRunner().invoke(qt.cli.app, ["data", "update", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "连续 20 只" in result.output
    assert isinstance(result.exception, SystemExit)


def test_cli_bad_config_is_a_chinese_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("", encoding="utf-8")
    code, output = _run_cli(monkeypatch, _cli_source(), tmp_path, "--config", str(cfg))
    assert code == 1
    assert "无法读取配置文件" in output


def test_cli_symbols_run_writes_partial_failures_and_keeps_full_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    full_record = tmp_path / "update-failures.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    full_record.write_text('{"600009": "上次全量运行的记录"}', encoding="utf-8")
    code, output = _run_cli(
        monkeypatch,
        _cli_source({"600001"}),
        tmp_path,
        "--symbols",
        "600000,600001,600002,600003,600004",
    )
    assert code == 0, output
    assert full_record.read_text(encoding="utf-8") == '{"600009": "上次全量运行的记录"}'
    partial = json.loads((tmp_path / "update-failures-partial.json").read_text(encoding="utf-8"))
    assert list(partial) == ["600001"]
    assert "update-failures-partial.json" in output


# ---------------------------------------------------------------- qt data check


def test_data_check_prints_counts_and_writes_csv(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write(Table.TRADE_DAYS, make_trade_days(DAYS))
    bars = make_bars("600000", DAYS, [10.0] * 5 + [13.0] * 5)
    bars.loc[1, "open"] = 0.0
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", LISTED, None)]),
        bars=[bars],
    )
    result = CliRunner().invoke(qt.cli.app, ["data", "check", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "开盘价不为正：1" in result.output
    assert "复权收盘价单日涨跌超过 21%：1" in result.output
    written = pd.read_csv(tmp_path / "history-anomalies.csv", dtype={"symbol": str})
    assert sorted(written["kind"]) == ["non_positive_open", "price_jump"]
    assert written["symbol"].tolist() == ["600000", "600000"]


def test_data_check_clean_store_and_empty_store(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "ok")
    store.write(Table.TRADE_DAYS, make_trade_days(DAYS))
    populate_store(store, bars=[make_bars("600000", DAYS, [10.0] * 10)])
    ok = CliRunner().invoke(qt.cli.app, ["data", "check", "--data-dir", str(tmp_path / "ok")])
    assert ok.exit_code == 0, ok.output
    assert "未发现异常" in ok.output
    assert (tmp_path / "ok" / "history-anomalies.csv").exists()
    empty = CliRunner().invoke(qt.cli.app, ["data", "check", "--data-dir", str(tmp_path / "no")])
    assert empty.exit_code == 1
    assert "trade_days 表为空" in empty.output
