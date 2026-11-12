"""参数扫描：override、参数解析、笛卡尔积回测、样本内约束、命令行。"""

import csv
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

import qt.backtest.report as report_module
import qt.backtest.sweep as sweep_module
from qt.backtest.commands import backtest_app
from qt.backtest.engine import BacktestResult
from qt.backtest.metrics import compute_metrics
from qt.backtest.report import run_period
from qt.backtest.sweep import override, parse_param, run_sweep
from qt.calendar import TradingCalendar
from qt.cli import app
from qt.config import AppConfig, RiskConfig
from qt.costs import CostModel
from qt.data.store import ParquetStore
from qt.features import FeaturePanel
from qt.risk import RiskManager
from tests.fakes import (
    default_app_config,
    make_bars,
    make_securities,
    populate_store,
    weekdays,
    write_calendar,
)

D = weekdays(date(2024, 1, 2), 30)
DEFAULT_YAML = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
RESULT_COLUMNS = ["annual_return", "max_drawdown", "sharpe", "win_rate", "trade_count"]


def _store(tmp_path: Path) -> ParquetStore:
    store = ParquetStore(tmp_path / "store")
    write_calendar(store, D)
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", date(2010, 1, 1), None)]),
        bars=[make_bars("600000", D[:20], [10.0] * 20)],
    )
    return store


def _cfg() -> AppConfig:
    """样本内区间落在测试数据内；样本外起点放在数据末段，用来发现越界。"""
    cfg = default_app_config()
    backtest = cfg.backtest.model_copy(
        update={"in_sample_start": D[0], "in_sample_end": D[11], "out_sample_start": D[12]}
    )
    return cfg.model_copy(update={"backtest": backtest})


# ---------- override ----------


def test_override_returns_new_config_and_leaves_original() -> None:
    cfg = default_app_config()
    new = override(cfg, "strategy.min_dividend_yield", 0.05)
    assert new.strategy.min_dividend_yield == 0.05
    assert cfg.strategy.min_dividend_yield == 0.04


@pytest.mark.parametrize("path", ["strategy.no_such", "nosuch.min_dividend_yield", "strategy", ""])
def test_override_unknown_path_raises(path: str) -> None:
    with pytest.raises(ValueError, match="路径"):
        override(default_app_config(), path, 1)


@pytest.mark.parametrize(
    ("path", "raw", "expected"),
    [
        ("strategy.min_dividend_yield", "0.03", 0.03),
        ("strategy.min_dividend_years", "7", 7),
        ("strategy.take_profit_mode", "trailing", "trailing"),
        ("universe.min_listed_years", "5", 5),
    ],
)
def test_parse_param_coerces_by_field_type(path: str, raw: str, expected: object) -> None:
    name, values = parse_param(default_app_config(), f"{path}={raw}")
    assert name == path
    assert values == [expected]
    assert type(values[0]) is type(expected)


def test_parse_param_splits_on_commas_in_order() -> None:
    name, values = parse_param(default_app_config(), "strategy.stop_loss=0.2, 0.1,0.15")
    assert (name, values) == ("strategy.stop_loss", [0.2, 0.1, 0.15])


def test_override_invalid_literal_raises_naming_path() -> None:
    with pytest.raises(ValueError, match=r"strategy\.take_profit_mode"):
        override(default_app_config(), "strategy.take_profit_mode", "foo")


def test_override_wrong_type_raises_naming_path() -> None:
    with pytest.raises(ValueError, match=r"strategy\.min_dividend_yield"):
        override(default_app_config(), "strategy.min_dividend_yield", "abc")


@pytest.mark.parametrize(
    "text", ["strategy.stop_loss", "strategy.stop_loss=", "strategy.stop_loss=0.1,,0.2", "=0.1"]
)
def test_parse_param_malformed_raises(text: str) -> None:
    with pytest.raises(ValueError, match="--param"):
        parse_param(default_app_config(), text)


def test_parse_param_unknown_path_and_bad_value_raise() -> None:
    cfg = default_app_config()
    with pytest.raises(ValueError, match="路径"):
        parse_param(cfg, "strategy.no_such=1")
    with pytest.raises(ValueError, match=r"strategy\.take_profit_mode"):
        parse_param(cfg, "strategy.take_profit_mode=fixed,foo")


# ---------- run_sweep ----------


def test_sweep_runs_cartesian_product_in_given_order(tmp_path: Path) -> None:
    grid = {
        "strategy.min_dividend_yield": [0.03, 0.05],
        "risk.cooldown_days": [0, 60],
    }
    table = run_sweep(_cfg(), _store(tmp_path), grid)
    assert list(table.columns) == [
        "strategy.min_dividend_yield",
        "risk.cooldown_days",
        *RESULT_COLUMNS,
    ]
    assert len(table) == 4
    combos = list(
        zip(table["strategy.min_dividend_yield"], table["risk.cooldown_days"], strict=True)
    )
    assert combos == [(0.03, 0), (0.03, 60), (0.05, 0), (0.05, 60)]


def _spy_run_period(monkeypatch: pytest.MonkeyPatch) -> list[tuple[date, date]]:
    """替换 sweep 里的 run_period，记录每次收到的区间。"""
    seen: list[tuple[date, date]] = []
    real = sweep_module.run_period

    def spy(
        cfg: AppConfig,
        store: ParquetStore,
        start: date,
        end: date,
        last_day: date | None = None,
        panel: FeaturePanel | None = None,
    ) -> BacktestResult:
        seen.append((start, end))
        return real(cfg, store, start, end, last_day=last_day, panel=panel)

    monkeypatch.setattr(sweep_module, "run_period", spy)
    return seen


def test_sweep_never_touches_out_of_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    seen = _spy_run_period(monkeypatch)
    run_sweep(cfg, _store(tmp_path), {"strategy.stop_loss": [0.1, 0.2]})
    assert len(seen) == 2
    for start, end in seen:
        assert cfg.backtest.in_sample_start <= start <= end <= cfg.backtest.in_sample_end


def test_sweep_rejects_changing_sample_boundaries(tmp_path: Path) -> None:
    for path in ("backtest.in_sample_start", "backtest.in_sample_end", "backtest.out_sample_start"):
        with pytest.raises(ValueError, match="样本"):
            run_sweep(_cfg(), _store(tmp_path), {path: ["2030-01-01"]})


def test_sweep_rejects_empty_grid_and_empty_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="参数"):
        run_sweep(_cfg(), _store(tmp_path), {})
    with pytest.raises(ValueError, match=r"strategy\.stop_loss"):
        run_sweep(_cfg(), _store(tmp_path), {"strategy.stop_loss": []})


def test_sweep_validates_every_combination_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_run_period(monkeypatch)
    with pytest.raises(ValueError, match=r"strategy\.take_profit_mode"):
        run_sweep(_cfg(), _store(tmp_path), {"strategy.take_profit_mode": ["fixed", "foo"]})
    assert seen == []


def _count_panels(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """替换 report 与 sweep 里的 build_panel，每次调用记一笔。"""
    calls: list[int] = []
    real = report_module.build_panel

    def counting(
        store: ParquetStore, calendar: TradingCalendar, cfg: AppConfig, start: date, end: date
    ) -> FeaturePanel:
        calls.append(1)
        return real(store, calendar, cfg, start, end)

    monkeypatch.setattr(report_module, "build_panel", counting)
    monkeypatch.setattr(sweep_module, "build_panel", counting)
    return calls


def test_panel_built_once_for_strategy_and_risk_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_panels(monkeypatch)
    grid = {"strategy.stop_loss": [0.1, 0.2, 0.3], "risk.cooldown_days": [0, 60]}
    table = run_sweep(_cfg(), _store(tmp_path), grid)
    assert len(table) == 6
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("path", "values"),
    [("strategy.lookback_days", [5, 10, 15]), ("universe.min_listed_years", [1, 2, 3])],
)
def test_panel_rebuilt_per_combination_for_panel_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, values: list[int]
) -> None:
    calls = _count_panels(monkeypatch)
    run_sweep(_cfg(), _store(tmp_path), {path: values})
    assert len(calls) == 3


def test_each_combination_gets_fresh_risk_manager_and_matches_standalone_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[RiskManager] = []
    real_init = RiskManager.__init__

    def tracking(
        self: RiskManager, cfg: RiskConfig, calendar: TradingCalendar, costs: CostModel
    ) -> None:
        created.append(self)
        real_init(self, cfg, calendar, costs)

    store = _store(tmp_path)
    cfg = _cfg()
    grid = {"risk.cooldown_days": [0, 60], "risk.max_positions": [3, 10]}
    monkeypatch.setattr(RiskManager, "__init__", tracking)
    table = run_sweep(cfg, store, grid)
    monkeypatch.undo()
    assert len(created) == 4
    assert len({id(r) for r in created}) == 4
    for _, row in table.iterrows():
        one = override(
            override(cfg, "risk.cooldown_days", row["risk.cooldown_days"]),
            "risk.max_positions",
            row["risk.max_positions"],
        )
        result = run_period(one, store, cfg.backtest.in_sample_start, cfg.backtest.in_sample_end)
        m = compute_metrics(result.equity, result.trades)
        assert row["annual_return"] == pytest.approx(m.annual_return)
        assert row["trade_count"] == m.trade_count


# ---------- 命令行 ----------


def _cli(tmp_path: Path, *args: str) -> Result:
    cfg_path = tmp_path / "cfg.yaml"
    if not cfg_path.exists():
        raw = yaml.safe_load(DEFAULT_YAML.read_text("utf-8"))
        raw["backtest"]["in_sample_start"] = D[0]
        raw["backtest"]["in_sample_end"] = D[11]
        raw["backtest"]["out_sample_start"] = D[12]
        cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    if not (tmp_path / "store").exists():
        _store(tmp_path)
    return CliRunner().invoke(
        backtest_app,
        [
            "sweep",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "store"),
            "--out",
            str(tmp_path / "reports"),
            *args,
        ],
    )


def test_sweep_command_prints_table_and_writes_csv(tmp_path: Path) -> None:
    result = _cli(
        tmp_path,
        "--param",
        "strategy.stop_loss=0.1,0.2",
        "--param",
        "risk.cooldown_days=0,60",
    )
    assert result.exit_code == 0, result.output
    for label in ("年化收益", "最大回撤", "夏普", "胜率", "交易笔数", "strategy.stop_loss"):
        assert label in result.output
    (written,) = (tmp_path / "reports").glob("sweep-*.csv")
    assert re.fullmatch(r"sweep-\d{8}-\d{6}\.csv", written.name)
    assert str(written) in result.output
    with written.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["strategy.stop_loss", "risk.cooldown_days", *RESULT_COLUMNS]
    assert len(rows) == 5


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--param", "strategy.stop_loss=0.1", "--param", "strategy.stop_loss=0.2"], "重复"),
        (["--param", "strategy.stop_loss"], "--param"),
        (["--param", "strategy.no_such=1"], "路径"),
        (["--param", "strategy.take_profit_mode=foo"], "strategy.take_profit_mode"),
        ([], "--param"),
    ],
)
def test_sweep_command_bad_param_gives_chinese_error(
    tmp_path: Path, args: list[str], message: str
) -> None:
    result = _cli(tmp_path, *args)
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "reports").exists()


def test_sweep_command_missing_config_and_data(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        backtest_app,
        ["sweep", "--param", "strategy.stop_loss=0.1", "--config", str(tmp_path / "no.yaml")],
    )
    assert result.exit_code == 1
    assert "配置文件" in result.output
    empty = ParquetStore(tmp_path / "empty")
    write_calendar(empty, D)
    result = CliRunner().invoke(
        backtest_app,
        [
            "sweep",
            "--param",
            "strategy.stop_loss=0.1",
            "--data-dir",
            str(tmp_path / "empty"),
            "--out",
            str(tmp_path / "reports"),
        ],
    )
    assert result.exit_code == 1
    assert "没有日线数据" in result.output
    assert "Traceback" not in result.output


# ---------- 根命令装配 ----------


@pytest.mark.parametrize(
    "args",
    [["backtest", "run", "--help"], ["backtest", "sweep", "--help"], ["data", "update", "--help"]],
)
def test_root_app_exposes_commands(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
