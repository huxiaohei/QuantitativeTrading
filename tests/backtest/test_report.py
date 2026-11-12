"""HTML 报告、周期解析、run_period 与命令行测试。"""

import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from qt.backtest.commands import backtest_app
from qt.backtest.engine import BacktestResult
from qt.backtest.metrics import Metrics, benchmark_metrics, compute_metrics
from qt.backtest.report import (
    BenchmarkSet,
    load_benchmarks,
    render_report,
    resolve_period,
    run_period,
    verdict_sentence,
)
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.models import ExitReason, Order, Position, Side, Trade
from qt.risk import Rejection, RiskEvent, RiskEventKind
from tests.fakes import (
    default_app_config,
    make_bars,
    make_benchmark,
    make_candidate,
    make_securities,
    populate_store,
    weekdays,
    write_calendar,
)

D = weekdays(date(2024, 1, 2), 30)
GAPS = Path(__file__).resolve().parent.parent.parent / "docs" / "data-gaps.md"
ETF = "510880"


def _equity(values: list[float]) -> "pd.Series[float]":
    return pd.Series(values, index=pd.Index(D[: len(values)]), dtype="float64")


def _trade(symbol: str, pnl: float, reason: ExitReason) -> Trade:
    return Trade(
        symbol=symbol,
        entry_day=D[1],
        exit_day=D[10],
        cost=10_000.0,
        proceeds=10_000.0 + pnl,
        exit_reason=reason,
        holding_days=9,
        entry_metrics={"dividend_yield": 0.052, "price_position": 0.12},
    )


def _result(values: list[float] | None = None) -> BacktestResult:
    cand = make_candidate("600009")
    equity = _equity(values or [150_000.0, 151_000.0, 149_000.0, 153_000.0, 152_000.0])
    return BacktestResult(
        equity=equity,
        cash=equity.copy(),
        trades=[
            _trade("600001", 1200.0, ExitReason.TAKE_PROFIT_FIXED),
            _trade("600002", -1500.0, ExitReason.STOP_LOSS),
        ],
        open_positions=[
            Position("600003", 1000.0, 10_000.0, D[2], "银行", {"dividend_yield": 0.05})
        ],
        rejections=[Rejection(cand, "cooldown", "冷却期"), Rejection(cand, "cooldown", "冷却期")],
        risk_events=[RiskEvent(D[3], RiskEventKind.WARN, -0.16, "回撤预警")],
        expired_buys=[Order("600004", Side.BUY, 100.0, D[0], "测试")],
    )


def _benchmarks(codes: tuple[str, ...] = ("H00300", ETF)) -> dict[str, "pd.Series[float]"]:
    return {c: _equity([100.0, 101.0, 100.5, 102.0, 103.0]) for c in codes}


def _render(
    tmp_path: Path,
    *,
    result: BacktestResult | None = None,
    metrics: Metrics | None = None,
    benchmarks: dict[str, "pd.Series[float]"] | None = None,
    gaps: Path = GAPS,
) -> str:
    result = result or _result()
    metrics = metrics or compute_metrics(result.equity, result.trades)
    bench = _benchmarks() if benchmarks is None else benchmarks
    out = render_report(
        result,
        metrics,
        bench,
        default_app_config(),
        "in",
        gaps,
        tmp_path / "r.html",
        last_closes={"600003": 11.0},
    )
    return out.read_text(encoding="utf-8")


# ---------- 报告内容 ----------


def test_report_contains_required_sections(tmp_path: Path) -> None:
    html = _render(tmp_path)
    for text in ("数据局限", "红利 ETF", "未扣红利税", "600001", "600002", "固定止盈", "价格止损"):
        assert text in html
    for text in ("沪深300 全收益", "风控事件", "回撤预警", "本次使用的全部参数", "initial_cash"):
        assert text in html
    assert "<svg" in html
    body = re.sub(r"<(pre|svg).*?</\1>", "", html, flags=re.S)  # 原文与图形不检查
    assert not re.search(r"\b(nan|NaN|None)\b", body)


def test_report_numbers_are_formatted(tmp_path: Path) -> None:
    html = _render(tmp_path)
    assert "150,000.00" in html or "10,000.00" in html  # 金额带千分位
    assert re.search(r"\d+\.\d{2}%", html)  # 百分比两位小数


def test_report_flags_drawdown_near_limit(tmp_path: Path) -> None:
    banner = "最大回撤接近 50% 上限，按设计文档策略需要重做"
    bad = _render(tmp_path, metrics=Metrics(0.0, -0.41, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
    assert banner in bad
    assert bad.index(banner) < bad.index("概要")  # 位于报告最顶部
    edge = _render(tmp_path, metrics=Metrics(0.0, -0.40, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
    assert banner in edge
    ok = _render(tmp_path, metrics=Metrics(0.0, -0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
    assert "策略需要重做" not in ok


def test_report_is_self_contained(tmp_path: Path) -> None:
    html = _render(tmp_path)
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", html)
    assert "<link" not in html
    assert not re.search(r"<script[^>]*src", html)
    assert "http://" not in html
    assert "https://" not in html


def test_missing_benchmark_is_omitted_with_note(tmp_path: Path) -> None:
    html = _render(tmp_path, benchmarks=_benchmarks(("H00300",)))
    assert "红利 ETF (510880)" in html  # 出现在缺失说明里
    assert "缺少数据" in html
    assert "无法比较" in html


def test_all_fixed_limitation_statements_present(tmp_path: Path) -> None:
    html = _render(tmp_path)
    for text in (
        "分红按除权价再投资且未扣红利税",
        "财务指标为最新口径，已被追溯调整的历史数值可能与当时公布的不同",
        "没有实际披露日期的财报按法定披露截止日使用，对延迟披露的公司偏乐观",
        "退市股票按最后一个交易日收盘价清算",
        "涨跌停与停牌按日线开盘价近似判断",
        "回测结果不代表未来收益",
    ):
        assert f"<li>{text}</li>" in html


def test_data_gaps_embedded_verbatim(tmp_path: Path) -> None:
    gaps = tmp_path / "gaps.md"
    gaps.write_text("# 缺口\n\n- 特殊 <标记> & 内容\n", encoding="utf-8")
    html = _render(tmp_path, gaps=gaps)
    assert "# 缺口" in html
    assert "特殊 &lt;标记&gt; &amp; 内容" in html  # 转义但不丢内容


def test_open_positions_and_risk_sections(tmp_path: Path) -> None:
    html = _render(tmp_path)
    assert "期末持仓" in html
    assert "600003" in html
    assert "10.00%" in html  # 1000 股 * 11 / 10000 - 1
    assert "不计入上述拒绝统计" in html
    assert "cooldown" in html
    assert "作废的买单" in html


@pytest.mark.parametrize(
    ("strategy", "etf", "expected"),
    [
        ((0.10, -0.30), (0.05, -0.20), "年化收益高于红利 ETF，最大回撤也更大"),
        ((0.10, -0.10), (0.05, -0.20), "年化收益高于红利 ETF，最大回撤更小"),
        ((0.02, -0.30), (0.05, -0.20), "年化收益低于红利 ETF，最大回撤更大"),
        ((0.02, -0.10), (0.05, -0.20), "年化收益低于红利 ETF，最大回撤也更小"),
    ],
)
def test_verdict_sentence_combinations(
    strategy: tuple[float, float], etf: tuple[float, float], expected: str
) -> None:
    s = Metrics(strategy[0], strategy[1], 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    e = Metrics(etf[0], etf[1], 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    assert expected in verdict_sentence(s, e)


def test_verdict_unavailable_without_etf() -> None:
    s = Metrics(0.1, -0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    assert "无法比较" in verdict_sentence(s, None)


def test_benchmark_metrics_used_in_summary(tmp_path: Path) -> None:
    html = _render(tmp_path)
    expected = f"{benchmark_metrics(_benchmarks()[ETF]).annual_return:.2%}"
    assert expected in html


# ---------- 周期、基准加载、run_period ----------


def _store(tmp_path: Path, n_bars: int = 12) -> ParquetStore:
    store = ParquetStore(tmp_path / "store")
    write_calendar(store, D)
    populate_store(
        store,
        securities=make_securities([("600000", "sh_main", date(2010, 1, 1), None)]),
        bars=[make_bars("600000", D[:n_bars], [10.0] * n_bars)],
    )
    return store


def test_resolve_period_all_three(tmp_path: Path) -> None:
    cfg = default_app_config()
    store = _store(tmp_path)
    last = D[11]
    assert resolve_period(cfg, store, "in") == (
        cfg.backtest.in_sample_start,
        cfg.backtest.in_sample_end,
    )
    assert resolve_period(cfg, store, "out") == (cfg.backtest.out_sample_start, last)
    assert resolve_period(cfg, store, "full") == (cfg.backtest.in_sample_start, last)
    with pytest.raises(ValueError, match="period"):
        resolve_period(cfg, store, "bogus")


def test_run_period_clamps_to_calendar_and_bars(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = run_period(default_app_config(), store, date(2023, 1, 1), date(2030, 1, 1))
    assert list(result.equity.index) == D[:12]
    assert result.equity.iloc[0] == pytest.approx(default_app_config().backtest.initial_cash)


def test_run_period_without_data_raises_chinese_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="没有日线数据"):
        run_period(default_app_config(), store, date(2030, 1, 1), date(2030, 6, 1))


def test_load_benchmarks_aligns_and_notes_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        Table.BENCHMARKS, make_benchmark("H00300", [D[0], D[2], D[3]], [100.0, 102.0, 103.0])
    )
    index = pd.Index(D[:5])
    found: BenchmarkSet = load_benchmarks(store, default_app_config(), index)
    series = found.series["H00300"]
    assert list(series.index) == D[:5]
    assert series.loc[D[1]] == 100.0  # 前向填充
    assert series.loc[D[4]] == 103.0
    assert found.missing == [ETF]


# ---------- 命令行 ----------


def test_run_command_writes_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(Table.BENCHMARKS, make_benchmark("H00300", D[:12], [100.0 + i for i in range(12)]))
    raw = yaml.safe_load((GAPS.parent.parent / "config" / "default.yaml").read_text("utf-8"))
    raw["backtest"]["in_sample_start"] = D[0]
    raw["backtest"]["in_sample_end"] = D[11]
    raw["backtest"]["out_sample_start"] = D[6]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    out_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        backtest_app,
        [
            "run",
            "--period",
            "in",
            "--config",
            str(cfg_path),
            "--data-dir",
            str(tmp_path / "store"),
            "--out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    (written,) = out_dir.glob("backtest-in-*.html")
    assert re.fullmatch(r"backtest-in-\d{8}-\d{6}\.html", written.name)
    assert str(written) in result.output
    assert "年化收益" in result.output
    assert "最大回撤" in result.output
    assert "数据局限" in written.read_text(encoding="utf-8")


def test_run_command_reports_missing_data(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "empty")
    write_calendar(store, D)
    result = CliRunner().invoke(
        backtest_app,
        ["run", "--period", "full", "--data-dir", str(tmp_path / "empty"), "--out", str(tmp_path)],
        env={},
    )
    assert result.exit_code == 1
    assert "没有日线数据" in result.output
