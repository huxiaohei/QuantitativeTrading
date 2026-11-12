"""参数扫描：override、参数解析、笛卡尔积回测、样本内约束、命令行。"""

import csv
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd
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
from qt.config import AppConfig
from qt.costs import CostModel
from qt.data.store import ParquetStore
from qt.features import FeaturePanel
from qt.models import ExitReason
from qt.risk import RiskEventKind, RiskManager
from qt.strategy.base import Strategy
from tests.fakes import (
    default_app_config,
    make_bars,
    make_securities,
    populate_store,
    populate_trading_store,
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


# ---------- 真实策略有成交的数据集：隔离、面板重建、样本内约束 ----------


class _Trading:
    """交易数据集与样本内配置：样本内到第 360 个交易日，之后还有 160 天含暴跌的样本外行情。"""

    def __init__(self, tmp_path: Path) -> None:
        self.store = ParquetStore(tmp_path / "trading")
        self.days = populate_trading_store(self.store)
        cfg = default_app_config()
        backtest = cfg.backtest.model_copy(
            update={
                "in_sample_start": self.days[0],
                "in_sample_end": self.days[360],
                "out_sample_start": self.days[361],
            }
        )
        self.cfg = cfg.model_copy(update={"backtest": backtest})

    def sweep(
        self, grid: dict[str, list[object]]
    ) -> tuple[pd.DataFrame, list[tuple[Mapping[str, object], BacktestResult]]]:
        """运行扫描，同时收集每组参数的完整回测结果。"""
        got: list[tuple[Mapping[str, object], BacktestResult]] = []
        table = run_sweep(
            self.cfg, self.store, grid, on_result=lambda ov, res: got.append((ov, res))
        )
        return table, got

    def standalone(self, overrides: Mapping[str, object]) -> BacktestResult:
        """用同一组覆盖后的配置单独跑一次样本内回测。"""
        one = self.cfg
        for path, value in overrides.items():
            one = override(one, path, value)
        bt = self.cfg.backtest
        return run_period(one, self.store, bt.in_sample_start, bt.in_sample_end)


def _assert_same_result(a: BacktestResult, b: BacktestResult) -> None:
    pd.testing.assert_series_equal(a.equity, b.equity)
    assert a.trades == b.trades
    assert a.risk_events == b.risk_events
    assert [p.symbol for p in a.open_positions] == [p.symbol for p in b.open_positions]


def _exit_reasons(result: BacktestResult) -> set[ExitReason]:
    return {t.exit_reason for t in result.trades}


def _paused(result: BacktestResult) -> bool:
    return any(e.kind is RiskEventKind.PAUSE for e in result.risk_events)


def test_trading_fixture_really_trades_under_default_config(tmp_path: Path) -> None:
    """夹具本身：默认配置下真实策略有止盈、止损，并且买入的是四只候选股。"""
    t = _Trading(tmp_path)
    result = t.standalone({})
    assert {ExitReason.TAKE_PROFIT_FIXED, ExitReason.STOP_LOSS} <= _exit_reasons(result)
    assert {"600001", "600002"} <= {x.symbol for x in result.trades}
    assert len(result.open_positions) >= 2


def test_every_combination_equals_standalone_run_on_all_outputs(tmp_path: Path) -> None:
    t = _Trading(tmp_path)
    grid: dict[str, list[object]] = {
        "risk.pause_drawdown": [0.01, 0.20],
        "risk.cooldown_days": [60, 0],
        "strategy.take_profit_mode": ["fixed", "trailing"],
    }
    table, got = t.sweep(grid)
    assert len(got) == len(table) == 8
    for (overrides, result), (_, row) in zip(got, table.iterrows(), strict=True):
        alone = t.standalone(overrides)
        _assert_same_result(result, alone)
        m = compute_metrics(alone.equity, alone.trades)
        assert row["annual_return"] == pytest.approx(m.annual_return)
        assert row["max_drawdown"] == pytest.approx(m.max_drawdown)
        assert row["sharpe"] == pytest.approx(m.sharpe)
        assert row["win_rate"] == pytest.approx(m.win_rate)
        assert row["trade_count"] == m.trade_count
    # 数据集必须真的覆盖这些场景，否则测试会退化成零成交
    results = [res for _, res in got]
    assert any(_paused(r) for r in results)
    assert any(ExitReason.STOP_LOSS in _exit_reasons(r) for r in results)
    assert any(ExitReason.TAKE_PROFIT_FIXED in _exit_reasons(r) for r in results)
    assert len({repr(r.trades) for r in results}) >= 3


def test_risk_state_does_not_leak_to_later_combinations(tmp_path: Path) -> None:
    """先出现暂停与止损冷却的组合，后面的组合必须不受影响。"""
    t = _Trading(tmp_path)
    grid: dict[str, list[object]] = {
        "risk.pause_drawdown": [0.01, 0.20],
        "risk.cooldown_days": [60, 0],
    }
    _, got = t.sweep(grid)
    first, last = got[0][1], got[3][1]
    assert _paused(first)
    assert ExitReason.STOP_LOSS in _exit_reasons(first)
    assert not _paused(last)
    # 冷却期 0 且没有暂停：止损后的 600002 在次日重新买入，所以有两笔止损
    assert [x.symbol for x in last.trades].count("600002") == 2
    _assert_same_result(last, t.standalone(got[3][0]))
    assert last.trades != first.trades


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("strategy.lookback_days", [250, 60]),
        ("universe.min_listed_years", [3, 30]),
        ("universe.min_avg_amount_60d", [5e7, 5e9]),
    ],
)
def test_rebuilt_panel_uses_the_overridden_config(
    tmp_path: Path, path: str, values: list[object]
) -> None:
    t = _Trading(tmp_path)
    _, got = t.sweep({path: values})
    for overrides, result in got:
        _assert_same_result(result, t.standalone(overrides))
    # 两组参数的结果必须不同；若重建面板时误用基础配置，它们会一模一样
    (_, a), (_, b) = got
    assert a.trades != b.trades or not a.equity.equals(b.equity)


def test_sweep_effective_period_stays_inside_in_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = _Trading(tmp_path)
    bt = t.cfg.backtest
    # 前提：样本外确实有数据，而且一旦纳入会改变结果（暴跌触发的止损发生在样本外）
    whole = run_period(t.cfg, t.store, bt.in_sample_start, t.days[-1])
    assert any(x.exit_day > bt.in_sample_end for x in whole.trades)
    reached: list[tuple[date, date]] = []
    real = report_module.run_backtest

    def spy(
        panel: FeaturePanel,
        strategy: Strategy,
        risk: RiskManager,
        costs: CostModel,
        calendar: TradingCalendar,
        start: date,
        end: date,
        initial_cash: float,
        buy_order_valid_days: int = 3,
    ) -> BacktestResult:
        reached.append((start, end))
        return real(
            panel, strategy, risk, costs, calendar, start, end, initial_cash, buy_order_valid_days
        )

    monkeypatch.setattr(report_module, "run_backtest", spy)
    grid: dict[str, list[object]] = {
        "strategy.stop_loss": [0.10, 0.15],
        "risk.cooldown_days": [0, 60],
    }
    _, got = t.sweep(grid)
    assert len(reached) == 4
    for start, end in reached:
        assert start == bt.in_sample_start
        assert end == bt.in_sample_end
    for _, result in got:
        assert result.equity.index[0] >= bt.in_sample_start
        assert result.equity.index[-1] == bt.in_sample_end
        for trade in result.trades:
            assert bt.in_sample_start <= trade.entry_day <= trade.exit_day <= bt.in_sample_end


def test_override_error_message_is_chinese_and_keeps_path() -> None:
    with pytest.raises(ValueError, match="无效") as info:
        override(default_app_config(), "strategy.min_dividend_years", "60.5")
    assert "strategy.min_dividend_years" in str(info.value)


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
    assert "共 4 组参数" in result.output
    for done in range(1, 5):
        assert f"[进度] 已完成 {done}/4 组" in result.output


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


@pytest.mark.parametrize("command", ["sweep", "run"])
def test_nonexistent_data_dir_gives_chinese_error(tmp_path: Path, command: str) -> None:
    args = ["--param", "strategy.stop_loss=0.1"] if command == "sweep" else []
    result = CliRunner().invoke(
        backtest_app,
        [command, *args, "--data-dir", str(tmp_path / "no-such-dir"), "--out", str(tmp_path / "o")],
    )
    assert result.exit_code == 1
    assert "qt data update" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
