"""回测报告：周期解析、run_period、基准加载、图表与 HTML 渲染。

报告是单个自包含 HTML 文件：图用 matplotlib（Agg 后端）生成 SVG 后内嵌，不引用任何外部资源。
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import matplotlib
import pandas as pd
from jinja2 import Environment, PackageLoader
from matplotlib import font_manager
from matplotlib.figure import Figure

from qt.backtest.engine import BacktestResult, run_backtest
from qt.backtest.metrics import (
    Metrics,
    benchmark_metrics,
    exit_breakdown,
    rejection_breakdown,
)
from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.costs import CostModel
from qt.data.check import ANOMALY_FILE
from qt.data.check import KIND_LABELS as ANOMALY_LABELS
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.data.update import FAILURES_FILE, count_delist_missing, count_no_bars, load_calendar
from qt.features import FeaturePanel, build_panel
from qt.risk import RiskManager
from qt.strategy.high_dividend_low import HighDividendLow

matplotlib.use("Agg")

ETF_CODE = "510880"
BENCHMARK_NAMES = {"H00300": "沪深300 全收益", ETF_CODE: "红利 ETF (510880)"}
PERIOD_LABELS = {"in": "样本内", "out": "样本外", "full": "全区间"}
EXIT_LABELS = {
    "take_profit_fixed": "固定止盈",
    "take_profit_trailing": "移动止盈",
    "stop_loss": "价格止损",
    "time_stop": "时间止损",
    "fundamental": "基本面止损",
    "delisted": "退市清算",
}
RULE_LABELS = {
    "paused": "暂停买入期间",
    "already_held": "已持有",
    "cooldown": "卖出后冷却期内",
    "max_positions": "持仓数已满",
    "industry_cap": "行业集中度上限",
    "lot_too_expensive": "一手也买不起",
    "no_cash": "现金不足",
}
KIND_LABELS = {"warn": "回撤预警", "pause": "暂停买入", "resume": "恢复买入"}
# 每条都写明对回测结果的偏差方向；印花税一条由 limitation_statements 按配置生成
FIXED_LIMITATIONS = [
    "分红按除权价再投资，未扣红利税（持股不足一年实际税率 10% 至 20%）——偏乐观",
    "分红再投资产生的不足一股的零碎股数在回测中被保留——影响很小",
    "股息率取“最近完整分红年度”与“当前年度已公告分红”中较大者（每股金额按当前股本折算）；"
    "削减分红的公司要到次年 9 月 1 日才被识别——对削减分红的公司偏乐观；"
    "数据源不区分常规分红与一次性特别分红，特别分红同样会抬高股息率、解除“削减分红”判定"
    "——偏乐观",
    "财务指标为最新口径，被追溯调整过的历史数值可能与当时公布的不同——方向不定",
    "没有实际披露日期的财报按法定披露截止日使用——对延迟披露的公司偏乐观",
    "退市股票按最后一个交易日收盘价清算，退市后无法卖出或价值归零的情况未计——偏乐观",
    "涨跌停与停牌按日线开盘价近似判断；ST 股票涨跌幅一律按 5% 处理，"
    "无涨跌幅限制的交易日未单独建模——方向不定",
    "期末仍持有的股票按最后收盘价计值，未扣卖出成本——略偏乐观",
    "历史 ST 状态与历史行业分类无法还原，使用当前信息——偏乐观",
    "回测结果不代表未来收益",
]
# 印花税一条插在 FIXED_LIMITATIONS 中的位置（第 8 条）
_STAMP_TAX_POSITION = 7
DRAWDOWN_BANNER = "最大回撤接近 50% 上限，按设计文档策略需要重做"
DRAWDOWN_BANNER_LEVEL = -0.40
TIE_THRESHOLD = 0.0005  # 年化收益或回撤幅度相差不足 0.05 个百分点视为持平
# 买入时指标快照的中文名与格式：pct 为百分比，int 为整数
METRIC_LABELS = {
    "dividend_yield": ("股息率", "pct"),
    "price_position": ("价格分位", "pct"),
    "dividend_years": ("连续分红年数", "int"),
    "roe_avg3": ("三年平均 ROE", "pct"),
    "payout_ratio": ("分红率", "pct"),
    "profit_yoy": ("净利润同比", "pct"),
    "debt_ratio": ("资产负债率", "pct"),
}
CJK_FONTS = ("PingFang SC", "Heiti TC", "Songti SC", "Arial Unicode MS")


class BacktestInputError(ValueError):
    """回测输入（日期区间、已存数据）不满足要求。"""


@dataclass(frozen=True)
class DataChecks:
    """报告第 8 节用到的数据检查结果。

    delisted 为股票池板块内已退市的证券数，delisted_with_bars 为其中有日线的数量；
    no_bars、delist_missing 取自最近一次全量更新的 update-failures.json（没有该文件为 None）；
    anomalies 为 qt data check 的异常数量（按类型），尚未扫描为 None。
    """

    delisted: int
    delisted_with_bars: int
    no_bars: int | None
    delist_missing: int | None
    anomalies: dict[str, int] | None


@dataclass(frozen=True)
class BenchmarkSet:
    """已对齐到回测交易日的基准收盘价，以及缺失的基准代码。"""

    series: dict[str, "pd.Series[float]"]
    missing: list[str]


# ---------- 周期与回测 ----------


def last_bar_day(store: ParquetStore) -> date | None:
    """存储中所有股票日线的最后一个交易日；没有日线返回 None。"""
    days: list[date] = []
    for symbol in store.read(Table.SECURITIES)["symbol"]:
        day = store.last_day(Table.BARS, str(symbol))
        if day is not None:
            days.append(day)
    return max(days, default=None)


def resolve_period(
    cfg: AppConfig, store: ParquetStore, period: str, last_day: date | None = None
) -> tuple[date, date]:
    """把 in/out/full 解析成 (开始, 结束)；out 与 full 的结束日取最后一个有日线的交易日。

    last_day 是调用方已查好的最后日线日，传入可避免重复扫描存储。
    """
    bt = cfg.backtest
    if period == "in":
        return bt.in_sample_start, bt.in_sample_end
    if period not in ("out", "full"):
        raise ValueError(f"period 只能是 in、out、full，收到 {period!r}")
    last = last_bar_day(store) if last_day is None else last_day
    if last is None:
        raise BacktestInputError("存储中没有日线数据，请先运行 qt data update")
    return (bt.out_sample_start if period == "out" else bt.in_sample_start), last


def clamp_period(
    store: ParquetStore, start: date, end: date, last_day: date | None = None
) -> tuple[TradingCalendar, date, date]:
    """把 [start, end] 收缩到日历交易日与最后一根日线之内；返回 (日历, 实际开始, 实际结束)。

    last_day 是调用方已查好的最后日线日，传入可避免重复扫描存储。
    """
    calendar = load_calendar(store)
    last = last_bar_day(store) if last_day is None else last_day
    days = calendar.between(start, min(end, last)) if last is not None else []
    if not days:
        raise BacktestInputError(f"{start} 至 {end} 之间没有日线数据，无法回测")
    return calendar, days[0], days[-1]


def run_period(
    cfg: AppConfig,
    store: ParquetStore,
    start: date,
    end: date,
    last_day: date | None = None,
    panel: FeaturePanel | None = None,
) -> BacktestResult:
    """在 [start, end] 上运行回测；区间收缩到日历交易日与最后一根日线之内。

    last_day 是调用方已查好的最后日线日，传入可避免重复扫描存储。
    panel 是调用方为同一收缩后区间预先建好的指标面板（参数扫描复用）；
    不传则在这里按 cfg 构建。
    """
    calendar, start, end = clamp_period(store, start, end, last_day)
    if panel is None:
        panel = build_panel(store, calendar, cfg, start, end)
    costs = CostModel(cfg.costs)
    strategy = HighDividendLow(cfg.strategy, cfg.universe)  # 想换策略，在这里替换
    risk = RiskManager(cfg.risk, calendar, costs)
    return run_backtest(
        panel,
        strategy,
        risk,
        costs,
        calendar,
        start,
        end,
        cfg.backtest.initial_cash,
        cfg.backtest.buy_order_valid_days,
    )


def load_benchmarks(store: ParquetStore, cfg: AppConfig, index: "pd.Index[Any]") -> BenchmarkSet:
    """读取各基准在回测区间内的收盘价，前向填充对齐到 index；完全没有数据的记入 missing。"""
    frame = store.read(Table.BENCHMARKS, start=index[0], end=index[-1])
    found: dict[str, pd.Series[float]] = {}
    missing: list[str] = []
    for code in cfg.backtest.benchmarks:
        rows = frame[frame["code"] == code]
        days = [pd.Timestamp(ts).date() for ts in rows["day"]]
        close = pd.Series(rows["close"].to_numpy(dtype="float64"), index=pd.Index(days))
        aligned = close.sort_index().reindex(close.index.union(index)).ffill().reindex(index)
        aligned = aligned.dropna()
        if len(aligned) == 0:
            missing.append(code)
        else:
            found[code] = aligned
    return BenchmarkSet(found, missing)


def load_last_closes(store: ParquetStore, symbols: list[str], end: date) -> dict[str, float]:
    """各股票截至 end 的最后收盘价，用于计算期末持仓的浮动收益。"""
    if not symbols:
        return {}
    bars = store.read(Table.BARS, symbols=symbols, end=end).sort_values("day")
    last = bars.groupby("symbol")["close"].last()
    return {str(s): float(c) for s, c in last.items()}


def load_data_checks(store: ParquetStore, cfg: AppConfig, data_dir: Path) -> DataChecks:
    """统计幸存者偏差相关数字，并读取 data_dir 下的 update-failures.json 与历史行情扫描结果。"""
    secs = store.read(Table.SECURITIES)
    delisted = secs[secs["board"].isin(cfg.universe.boards) & secs["delist_date"].notna()]
    with_bars = sum(1 for s in delisted["symbol"] if store.has(Table.BARS, str(s)))
    no_bars: int | None = None
    delist_missing: int | None = None
    failures_path = data_dir / FAILURES_FILE
    if failures_path.is_file():
        failed = cast("dict[str, str]", json.loads(failures_path.read_text(encoding="utf-8")))
        no_bars, delist_missing = count_no_bars(failed), count_delist_missing(failed)
    anomalies: dict[str, int] | None = None
    anomalies_path = data_dir / ANOMALY_FILE
    if anomalies_path.is_file():
        kinds = pd.read_csv(anomalies_path, usecols=["kind"], dtype={"kind": "str"})["kind"]
        anomalies = {str(k): int(n) for k, n in kinds.value_counts(sort=False).items()}
    return DataChecks(len(delisted), with_bars, no_bars, delist_missing, anomalies)


def limitation_statements(cfg: AppConfig) -> list[str]:
    """报告第 8 节逐条列出的局限；印花税一条按配置的税率分段生成。"""
    schedule = cfg.costs.stamp_tax_schedule

    def rate(value: float) -> str:
        return f"{value * 100:g}%"

    if len(schedule) == 1:
        segments = rate(schedule[0].rate)
    else:
        parts = [f"{schedule[1].start} 前 {rate(schedule[0].rate)}"]
        parts += [f"{p.start} 起 {rate(p.rate)}" for p in schedule[1:]]
        segments = "，".join(parts)
    stamp = f"印花税按历史税率分段计算（{segments}）；佣金与滑点为固定假设"
    out = list(FIXED_LIMITATIONS)
    out.insert(_STAMP_TAX_POSITION, stamp)
    return out


# ---------- 文字与格式 ----------


def verdict_sentence(strategy: Metrics, etf: Metrics | None) -> str:
    """分别在年化收益与最大回撤上给出相对红利 ETF 的明确好坏判断，不给操作建议。

    两项差异的绝对值小于 TIE_THRESHOLD 视为持平；回撤按幅度比较，幅度更小为更好。
    """
    if etf is None:
        return "红利 ETF 基准数据缺失，无法比较策略与直接持有红利 ETF 的差异。"
    ret_diff = strategy.annual_return - etf.annual_return
    dd_diff = abs(strategy.max_drawdown) - abs(etf.max_drawdown)
    if abs(ret_diff) < TIE_THRESHOLD:
        ret = "年化收益与红利 ETF 持平（持平）"
    else:
        word, grade = ("高于", "更好") if ret_diff > 0 else ("低于", "更差")
        ret = f"年化收益{word}红利 ETF（{grade}）"
    if abs(dd_diff) < TIE_THRESHOLD:
        dd = "最大回撤与红利 ETF 持平（持平）"
    else:
        word, grade = ("小于", "更好") if dd_diff < 0 else ("大于", "更差")
        dd = f"最大回撤{word}红利 ETF（{grade}）"
    return f"{ret}；{dd}。"


def _pct(value: float) -> str:
    return f"{value:.2%}"


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _snapshot_text(metrics: Mapping[str, float]) -> str:
    parts: list[str] = []
    for key, value in metrics.items():
        label, kind = METRIC_LABELS.get(key, (key, "num"))
        if math.isnan(value):
            parts.append(f"{label} —")
            continue
        text = (
            _pct(value) if kind == "pct" else (f"{value:.0f}" if kind == "int" else f"{value:.4g}")
        )
        parts.append(f"{label} {text}")
    return "；".join(parts)


def _flatten(data: Mapping[str, object], prefix: str = "") -> list[tuple[str, str]]:
    """把嵌套配置展开成 (a.b.c, 值) 列表。"""
    rows: list[tuple[str, str]] = []
    for key, item in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            rows.extend(_flatten(cast("dict[str, object]", item), name))
        else:
            rows.append((name, str(item)))
    return rows


@dataclass(frozen=True)
class CommonCurves:
    """共同对比区间内的策略净值与基准收盘价（均为原值，未归一）。"""

    start: date  # 共同区间起点：策略与各基准中最晚的首个可用日
    end: date
    truncated: bool  # 共同区间是否短于回测区间
    equity: "pd.Series[float]"
    benchmarks: dict[str, "pd.Series[float]"]


def common_curves(
    equity: "pd.Series[float]", benchmarks: Mapping[str, "pd.Series[float]"]
) -> CommonCurves:
    """把策略与基准截到共同区间。

    早于回测区间的基准数据一律忽略；区间内没有任何数据的基准被剔除；
    不向前回填，只在区间内前向填充。
    """
    first, last = equity.index[0], equity.index[-1]
    usable: dict[str, pd.Series[float]] = {}
    for code, close in benchmarks.items():
        inside = close.loc[[first <= d <= last for d in close.index]].dropna().sort_index()
        if len(inside) > 0:
            usable[code] = inside
    start = max([first, *(c.index[0] for c in usable.values())])
    skip = sum(1 for d in equity.index if d < start)
    window = equity.iloc[skip:]
    aligned = {c: s.reindex(window.index).ffill().dropna() for c, s in usable.items()}
    return CommonCurves(start, last, start > first, window, aligned)


def normalised_curves(cc: CommonCurves) -> dict[str, "pd.Series[float]"]:
    """各曲线在共同起点归一为 1.0；策略的键为 "strategy"，基准的键为基准代码。"""
    curves = {"strategy": cc.equity, **cc.benchmarks}
    return {name: s / float(s.iloc[0]) for name, s in curves.items()}


# ---------- 图表 ----------


def _cjk_font() -> str | None:
    """macOS 上可用的中文字体名；一个都没有返回 None。"""
    available = {f.name for f in font_manager.fontManager.ttflist}
    return next((name for name in CJK_FONTS if name in available), None)


def _clean_svg(svg: str) -> str:
    """去掉 XML 声明、DOCTYPE、metadata 与命名空间声明，保证内嵌后不含任何外部网址。"""
    svg = re.sub(r"<\?xml.*?\?>", "", svg, flags=re.S)
    svg = re.sub(r"<!DOCTYPE.*?>", "", svg, flags=re.S)
    svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
    return re.sub(r'\s+xmlns(:\w+)?="[^"]*"', "", svg).strip()


def chart_svg(equity: "pd.Series[float]", benchmarks: Mapping[str, "pd.Series[float]"]) -> str:
    """共同对比区间内的图：上图为起点归一的净值曲线，下图为回撤曲线；返回内嵌用的 SVG 文本。"""
    font = _cjk_font()
    zh = font is not None
    curves = normalised_curves(common_curves(equity, benchmarks))
    labels = {"strategy": "策略" if zh else "Strategy"}
    labels.update({c: BENCHMARK_NAMES.get(c, c) if zh else c for c in benchmarks})
    rc: dict[str, Any] = {"axes.unicode_minus": False}
    if font is not None:
        rc["font.sans-serif"] = [font]
    with cast("Any", matplotlib).rc_context(rc):  # 只在绘图期间改字体，不污染全局设置
        fig = Figure(figsize=(10, 7))
        top, bottom = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
        for name, curve in curves.items():
            days = list(curve.index)
            top.plot(days, curve.to_numpy(dtype="float64"), label=labels.get(name, name))
            drawdown = curve / curve.cummax() - 1
            bottom.plot(
                days, drawdown.to_numpy(dtype="float64") * 100, label=labels.get(name, name)
            )
        top.set_title("净值曲线（共同起点 = 1）" if zh else "Equity (common start = 1)")
        bottom.set_title("回撤（%）" if zh else "Drawdown (%)")
        top.legend(loc="upper left")
        for ax in (top, bottom):
            ax.grid(alpha=0.3)
        fig.tight_layout()
        buf = BytesIO()
        cast("Any", fig).savefig(buf, format="svg")  # savefig 的类型标注含未知类型，按 Any 调用
    return _clean_svg(buf.getvalue().decode("utf-8"))


# ---------- 渲染 ----------


def _environment() -> Environment:
    env = Environment(loader=PackageLoader("qt.backtest", "templates"), autoescape=True)
    env.filters["pct"] = _pct
    env.filters["money"] = _money
    return env


def _summary_rows(
    cc: CommonCurves, cfg: AppConfig
) -> tuple[list[dict[str, object]], Metrics | None]:
    """概要表各行（策略在前，基准按配置顺序），全部按共同区间计算；同时返回红利 ETF 的指标。"""
    rows: list[dict[str, object]] = [{"name": "策略", "m": benchmark_metrics(cc.equity)}]
    etf: Metrics | None = None
    for code in cfg.backtest.benchmarks:
        if code in cc.benchmarks:
            m = benchmark_metrics(cc.benchmarks[code])
            rows.append({"name": BENCHMARK_NAMES.get(code, code), "m": m})
            if code == ETF_CODE:
                etf = m
    return rows, etf


def _anomaly_rows(checks: DataChecks | None) -> list[tuple[str, int]] | None:
    """历史行情异常按类型的 (中文说明, 数量)；尚未扫描返回 None。"""
    if checks is None or checks.anomalies is None:
        return None
    known = [
        (ANOMALY_LABELS[k], checks.anomalies[k]) for k in ANOMALY_LABELS if k in checks.anomalies
    ]
    other = [(k, n) for k, n in checks.anomalies.items() if k not in ANOMALY_LABELS]
    return known + other


def render_report(
    result: BacktestResult,
    metrics: Metrics,
    benchmarks: Mapping[str, "pd.Series[float]"],
    cfg: AppConfig,
    period_label: str,
    data_gaps: Path,
    out: Path,
    last_closes: Mapping[str, float] | None = None,
    data_checks: DataChecks | None = None,
) -> Path:
    """渲染单文件 HTML 报告并写到 out，返回 out。

    benchmarks 为已对齐回测交易日的收盘价；配置里有但这里没有的基准在报告中给出缺失说明。
    last_closes 为期末持仓的最后收盘价，用于计算浮动收益（缺失显示为 —）。
    data_checks 为幸存者偏差统计与历史行情扫描结果（见 load_data_checks），不提供时报告注明未统计。
    """
    closes = last_closes or {}
    cc = common_curves(result.equity, benchmarks)
    rows, etf = _summary_rows(cc, cfg)
    trades = [
        {
            "t": t,
            "reason": EXIT_LABELS.get(t.exit_reason.value, t.exit_reason.value),
            "snapshot": _snapshot_text(t.entry_metrics),
        }
        for t in result.trades
    ]
    held = [
        {
            "p": p,
            "ret": p.ret(closes[p.symbol]) if p.symbol in closes else None,
            "snapshot": _snapshot_text(p.entry_metrics),
        }
        for p in result.open_positions
    ]
    exits = [
        {**r, "label": EXIT_LABELS.get(str(r["exit_reason"]), str(r["exit_reason"]))}
        for r in exit_breakdown(result.trades).to_dict("records")
    ]
    rejects = [
        {**r, "label": RULE_LABELS.get(str(r["rule"]), "")}
        for r in rejection_breakdown(result.rejections).to_dict("records")
    ]
    days = list(result.equity.index)
    html = (
        _environment()
        .get_template("report.html.j2")
        .render(
            banner=DRAWDOWN_BANNER if metrics.max_drawdown <= DRAWDOWN_BANNER_LEVEL else None,
            period=PERIOD_LABELS.get(period_label, period_label),
            first_day=days[0],
            last_day=days[-1],
            initial_cash=cfg.backtest.initial_cash,
            final_equity=float(result.equity.iloc[-1]),
            summary=rows,
            win_start=cc.start,
            win_end=cc.end,
            truncated=cc.truncated,
            verdict=verdict_sentence(benchmark_metrics(cc.equity), etf),
            missing=[
                BENCHMARK_NAMES.get(c, c) for c in cfg.backtest.benchmarks if c not in cc.benchmarks
            ],
            chart=chart_svg(result.equity, benchmarks),
            m=metrics,
            exits=exits,
            events=[
                {"e": e, "kind": KIND_LABELS.get(e.kind.value, e.kind.value)}
                for e in result.risk_events
            ],
            rejects=rejects,
            expired=len(result.expired_buys),
            trades=trades,
            held=held,
            params=_flatten(cfg.model_dump(mode="json")),
            gaps=data_gaps.read_text(encoding="utf-8"),
            limitations=limitation_statements(cfg),
            checks=data_checks,
            anomaly_rows=_anomaly_rows(data_checks),
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
