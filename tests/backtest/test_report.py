"""HTML 报告、周期解析、run_period 与命令行测试。"""

import re
from datetime import date
from pathlib import Path

import matplotlib
import pandas as pd
import pytest
import yaml
from markupsafe import escape
from typer.testing import CliRunner, Result

import qt.backtest.commands as commands_module
import qt.backtest.report as report_module
from qt.backtest.commands import backtest_app
from qt.backtest.engine import BacktestResult
from qt.backtest.metrics import Metrics, benchmark_metrics, compute_metrics
from qt.backtest.report import (
    BenchmarkSet,
    chart_svg,
    common_curves,
    load_benchmarks,
    normalised_curves,
    render_report,
    resolve_period,
    run_period,
    verdict_sentence,
)
from qt.calendar import TradingCalendar
from qt.costs import CostModel
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.features import FeaturePanel
from qt.models import ExitReason, Order, Position, Side, Trade
from qt.risk import Rejection, RiskEvent, RiskEventKind, RiskManager
from qt.strategy.base import Strategy
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


def external_refs(html: str) -> list[str]:
    """找出 HTML 中的外部资源引用；内嵌的数据局限原文（pre 块）不参与检查。"""
    body = re.sub(r"<pre>.*?</pre>", "", html, flags=re.S)
    patterns = {
        "script src": r"<script[^>]*\bsrc\s*=",
        "link": r"<link\b",
        "img http": r"<img[^>]*\bsrc\s*=\s*[\"']?\s*(https?:)?//",
        "src/href http": r"\b(src|href)\s*=\s*[\"']?\s*(https?:)?//",
        "css url": r"url\(\s*[\"']?\s*(https?:)?//",
        "bare url": r"https?://",
    }
    return [name for name, pat in patterns.items() if re.search(pat, body, flags=re.I)]


def test_report_is_self_contained(tmp_path: Path) -> None:
    assert external_refs(_render(tmp_path)) == []


def test_external_refs_checker_catches_violations() -> None:
    bad = {
        "script src": '<script src="x.js"></script>',
        "link": '<link rel="stylesheet" href="a.css">',
        "img http": '<img src="https://e.com/a.png">',
        "src/href http": '<a href="//cdn.example.com/x">x</a>',
        "css url": "<style>a { background: url(http://e.com/a.png) }</style>",
        "bare url": "<svg><text>http://e.com</text></svg>",
    }
    for name, html in bad.items():
        assert name in external_refs(html), name
    # pre 块里的网址文字不算外部引用
    assert external_refs("<pre>见 https://example.com</pre><p>ok</p>") == []


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
    held = html[html.index("期末持仓") : html.index("<h2>7.")]
    assert "600003" in held
    assert "<td>10.00%</td>" in held  # 1000 股 * 11 / 10000 - 1，位于持仓表的浮动收益列
    assert "<td>10.00%</td>" not in html[: html.index("期末持仓")]  # 不是别处偶然出现
    assert "不计入上述拒绝统计" in html
    assert "cooldown" in html
    assert "作废的买单" in html


def test_open_position_without_close_shows_dash(tmp_path: Path) -> None:
    result = _result()
    out = render_report(
        result,
        compute_metrics(result.equity, result.trades),
        _benchmarks(),
        default_app_config(),
        "in",
        GAPS,
        tmp_path / "r.html",
    )
    html = out.read_text(encoding="utf-8")
    held = html[html.index("期末持仓") : html.index("<h2>7.")]
    assert "<td>—</td>" in held


def test_nan_entry_metric_renders_dash(tmp_path: Path) -> None:
    result = _result()
    bad = Trade(
        "600001", D[1], D[10], 10_000.0, 11_000.0, ExitReason.TIME_STOP, 9,
        {"dividend_yield": float("nan"), "price_position": 0.12},
    )  # fmt: skip
    result = BacktestResult(result.equity, result.cash, [bad], [], [], [], [])
    html = _render(tmp_path, result=result)
    assert "股息率 —" in html
    assert "价格分位 12.00%" in html
    assert not re.search(r"nan", re.sub(r"<(pre|svg).*?</\1>", "", html, flags=re.S), flags=re.I)


BETTER, WORSE, TIE = "更好", "更差", "持平"


@pytest.mark.parametrize(
    ("ret_diff", "dd_strategy", "ret_text", "ret_judge", "dd_text", "dd_judge"),
    [
        (0.05, -0.10, "高于", BETTER, "小于", BETTER),
        (0.05, -0.20, "高于", BETTER, "与红利 ETF 持平", TIE),
        (0.05, -0.30, "高于", BETTER, "大于", WORSE),
        (0.0, -0.10, "与红利 ETF 持平", TIE, "小于", BETTER),
        (0.0, -0.20, "与红利 ETF 持平", TIE, "与红利 ETF 持平", TIE),
        (0.0, -0.30, "与红利 ETF 持平", TIE, "大于", WORSE),
        (-0.05, -0.10, "低于", WORSE, "小于", BETTER),
        (-0.05, -0.20, "低于", WORSE, "与红利 ETF 持平", TIE),
        (-0.05, -0.30, "低于", WORSE, "大于", WORSE),
    ],
)
def test_verdict_sentence_all_nine_combinations(
    ret_diff: float,
    dd_strategy: float,
    ret_text: str,
    ret_judge: str,
    dd_text: str,
    dd_judge: str,
) -> None:
    etf = Metrics(0.05, -0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    s = Metrics(0.05 + ret_diff, dd_strategy, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    ret_clause = f"年化收益{ret_text}" + ("" if ret_text.startswith("与") else "红利 ETF")
    dd_clause = f"最大回撤{dd_text}" + ("" if dd_text.startswith("与") else "红利 ETF")
    expected = f"{ret_clause}（{ret_judge}）；{dd_clause}（{dd_judge}）。"
    assert verdict_sentence(s, etf) == expected
    assert "也" not in verdict_sentence(s, etf)


def test_verdict_tie_threshold() -> None:
    etf = Metrics(0.0500, -0.2000, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    near = Metrics(0.0504, -0.2004, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    far = Metrics(0.0506, -0.2006, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    assert verdict_sentence(near, etf).count("持平") == 4
    assert "高于" in verdict_sentence(far, etf)
    assert "大于" in verdict_sentence(far, etf)


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


def test_run_period_passes_cfg_values_to_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, float] = {}
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
        seen["cash"] = initial_cash
        seen["days"] = buy_order_valid_days
        return real(
            panel, strategy, risk, costs, calendar, start, end, initial_cash, buy_order_valid_days
        )

    cfg = default_app_config()
    backtest = cfg.backtest.model_copy(
        update={"buy_order_valid_days": 7, "initial_cash": 123_456.0}
    )
    cfg = cfg.model_copy(update={"backtest": backtest})
    monkeypatch.setattr(report_module, "run_backtest", spy)
    run_period(cfg, _store(tmp_path), D[0], D[11])
    assert seen == {"cash": 123_456.0, "days": 7}


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
    raw["backtest"]["in_sample_end"] = D[5]  # 须早于 out_sample_start
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


# ---------- 共同对比区间 ----------


def _mid_bench() -> "pd.Series[float]":
    return pd.Series([50.0, 51.0, 52.0], index=pd.Index(D[2:5]), dtype="float64")


def test_common_curves_benchmark_starting_mid_range() -> None:
    equity = _equity([100.0, 110.0, 90.0, 99.0, 121.0])
    cc = common_curves(equity, {ETF: _mid_bench()})
    assert cc.start == D[2]
    assert cc.end == D[4]
    assert cc.truncated
    assert list(cc.equity.index) == D[2:5]
    assert cc.equity.tolist() == [90.0, 99.0, 121.0]
    curves = normalised_curves(cc)
    assert set(curves) == {"strategy", ETF}
    for series in curves.values():
        assert series.index[0] == D[2]
        assert series.iloc[0] == 1.0
    assert curves["strategy"].iloc[2] == pytest.approx(121.0 / 90.0)


def test_common_curves_full_coverage_is_untruncated() -> None:
    equity = _equity([100.0, 110.0, 90.0, 99.0, 121.0])
    cc = common_curves(equity, {ETF: _equity([1.0, 2.0, 3.0, 4.0, 5.0])})
    assert cc.start == D[0]
    assert not cc.truncated
    assert cc.equity.equals(equity)
    assert common_curves(equity, {}).start == D[0]


def test_common_curves_ignores_rows_before_equity_range() -> None:
    equity = _equity([100.0, 110.0, 90.0, 99.0, 121.0])
    before = weekdays(date(2023, 12, 20), 5)
    bench = pd.Series(
        [9.0] * 5 + [10.0, 11.0, 12.0, 13.0, 14.0],
        index=pd.Index(before + D[:5]),
        dtype="float64",
    )
    cc = common_curves(equity, {ETF: bench})
    assert cc.start == D[0]
    assert not cc.truncated
    assert list(cc.benchmarks[ETF].index) == D[:5]
    assert normalised_curves(cc)[ETF].iloc[1] == pytest.approx(1.1)
    # 只有早于回测区间的行：没有可用数据，不影响共同区间
    early = pd.Series([9.0] * 5, index=pd.Index(before), dtype="float64")
    assert common_curves(equity, {ETF: early}).benchmarks == {}


def _section1(html: str) -> str:
    return html[html.index("<h2>1.") : html.index("<h2>2.")]


def test_summary_table_uses_common_window(tmp_path: Path) -> None:
    result = _result([150_000.0, 165_000.0, 135_000.0, 148_500.0, 181_500.0])
    bench = {"H00300": _mid_bench(), ETF: _mid_bench()}
    html = _render(tmp_path, result=result, benchmarks=bench)
    section = _section1(html)
    truncated = benchmark_metrics(result.equity.iloc[2:])
    full = benchmark_metrics(result.equity)
    assert f"<td>{truncated.annual_return:.2%}</td>" in section
    assert f"<td>{truncated.max_drawdown:.2%}</td>" in section
    assert f"<td>{full.annual_return:.2%}</td>" not in section
    assert f"基准数据自 {D[2]} 起，对比区间短于回测区间" in section
    assert str(D[2]) in section.split("</h2>")[0]  # 标题写明对比区间
    assert str(D[4]) in section.split("</h2>")[0]
    full_metrics = compute_metrics(result.equity, result.trades)
    assert "回测全区间" in html[html.index("<h2>3.") :]
    assert f"{full_metrics.annual_return:.2%}" in html[html.index("<h2>3.") :]


def test_summary_table_without_truncation_has_no_note(tmp_path: Path) -> None:
    result = _result()
    html = _render(tmp_path, result=result)
    section = _section1(html)
    assert "对比区间短于回测区间" not in section
    full = benchmark_metrics(result.equity)
    assert f"<td>{full.annual_return:.2%}</td>" in section


def test_load_benchmarks_never_backfills_or_uses_rows_before_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = [
        make_benchmark("H00300", [D[0], D[1], D[3]], [100.0, 101.0, 103.0]),
        make_benchmark(ETF, [D[0], D[1]], [1.0, 2.0]),
    ]
    store.write(Table.BENCHMARKS, pd.concat(rows, ignore_index=True))
    found = load_benchmarks(store, default_app_config(), pd.Index(D[2:5]))
    assert list(found.series["H00300"].index) == [D[3], D[4]]  # D[2] 不回填
    assert found.series["H00300"].tolist() == [103.0, 103.0]
    assert found.missing == [ETF]  # 区间内没有任何行


# ---------- 图表与全局状态 ----------


def test_chart_svg_does_not_touch_global_rcparams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(matplotlib.rcParams, "axes.unicode_minus", True)
    monkeypatch.setitem(matplotlib.rcParams, "font.sans-serif", ["DejaVu Sans"])
    svg = chart_svg(_equity([100.0, 101.0, 99.0]), {})
    assert svg.startswith("<svg")
    assert matplotlib.rcParams["axes.unicode_minus"] is True
    assert list(matplotlib.rcParams["font.sans-serif"]) == ["DejaVu Sans"]


def test_real_data_gaps_embedded_in_full(tmp_path: Path) -> None:
    html = _render(tmp_path)
    lines = [ln.rstrip() for ln in GAPS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    for line in lines:
        assert str(escape(line)) in html


# ---------- 命令行 ----------


def _cli_files(tmp_path: Path) -> tuple[Path, Path]:
    """写一个把区间落在测试数据内的配置；返回 (配置路径, 输出目录)。"""
    store = _store(tmp_path)
    store.write(Table.BENCHMARKS, make_benchmark("H00300", D[:12], [100.0 + i for i in range(12)]))
    raw = yaml.safe_load((GAPS.parent.parent / "config" / "default.yaml").read_text("utf-8"))
    raw["backtest"]["in_sample_start"] = D[0]
    raw["backtest"]["in_sample_end"] = D[5]  # 须早于 out_sample_start
    raw["backtest"]["out_sample_start"] = D[6]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return cfg_path, tmp_path / "reports"


def _invoke(tmp_path: Path, *args: str) -> Result:
    return CliRunner().invoke(backtest_app, ["run", "--data-dir", str(tmp_path / "store"), *args])


@pytest.mark.parametrize(("period", "first", "last"), [("out", D[6], D[11]), ("full", D[0], D[11])])
def test_run_command_out_and_full(tmp_path: Path, period: str, first: date, last: date) -> None:
    cfg_path, out_dir = _cli_files(tmp_path)
    result = _invoke(tmp_path, "--period", period, "--config", str(cfg_path), "--out", str(out_dir))
    assert result.exit_code == 0, result.output
    (written,) = out_dir.glob(f"backtest-{period}-*.html")
    assert f"区间 {first} 至 {last}" in result.output
    assert "回测全区间" in result.output
    assert f"区间 {first} 至 {last}" in written.read_text(encoding="utf-8")


def test_run_command_computes_last_bar_day_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, out_dir = _cli_files(tmp_path)
    calls: list[int] = []
    real = report_module.last_bar_day

    def counting(store: ParquetStore) -> date | None:
        calls.append(1)
        return real(store)

    monkeypatch.setattr(report_module, "last_bar_day", counting)
    monkeypatch.setattr(commands_module, "last_bar_day", counting)
    result = _invoke(tmp_path, "--period", "full", "--config", str(cfg_path), "--out", str(out_dir))
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_run_command_missing_config_gives_chinese_error(tmp_path: Path) -> None:
    result = _invoke(tmp_path, "--config", str(tmp_path / "nope.yaml"), "--out", str(tmp_path))
    assert result.exit_code == 1
    assert "配置文件" in result.output
    assert "Traceback" not in result.output


def test_run_command_invalid_config_gives_chinese_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("universe: 1\n", encoding="utf-8")
    result = _invoke(tmp_path, "--config", str(bad), "--out", str(tmp_path))
    assert result.exit_code == 1
    assert "配置文件" in result.output
    assert "Traceback" not in result.output


def test_run_command_missing_data_gaps_gives_chinese_error(tmp_path: Path) -> None:
    cfg_path, out_dir = _cli_files(tmp_path)
    result = _invoke(
        tmp_path,
        "--config",
        str(cfg_path),
        "--out",
        str(out_dir),
        "--data-gaps",
        str(tmp_path / "nope.md"),
    )
    assert result.exit_code == 1
    assert "数据局限文档" in result.output
    assert "Traceback" not in result.output
    assert not out_dir.exists() or not list(out_dir.glob("*.html"))


def test_run_command_empty_config_gives_chinese_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "empty.yaml"
    cfg_path.write_text("", encoding="utf-8")
    result = _invoke(tmp_path, "--config", str(cfg_path), "--out", str(tmp_path / "reports"))
    assert result.exit_code == 1
    assert "无法读取配置文件" in result.output
    assert "Traceback" not in result.output
