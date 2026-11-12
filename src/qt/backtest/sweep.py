"""参数扫描：对参数网格的笛卡尔积逐一回测，只在样本内区间运行。

设计文档规定 2022 年之后的数据只用于检验、不参与调参。落实方式：run_sweep 没有区间参数，
区间固定取 cfg.backtest.in_sample_start 至 in_sample_end，且不允许扫描这两个边界。
"""

import itertools
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import cast

import pandas as pd
from pydantic import ValidationError

from qt.backtest.metrics import compute_metrics
from qt.backtest.report import clamp_period, last_bar_day, run_period
from qt.config import AppConfig
from qt.data.store import ParquetStore
from qt.features import FeaturePanel, build_panel

RESULT_COLUMNS = ["annual_return", "max_drawdown", "sharpe", "win_rate", "trade_count"]
# 扫描它们等于移动样本内外的分界线，一律禁止
_SAMPLE_BOUNDARIES = frozenset(
    {"backtest.in_sample_start", "backtest.in_sample_end", "backtest.out_sample_start"}
)
# 会改变指标面板的参数：面板只依赖股票池（universe.*）与回看天数
_PANEL_PARAM = "strategy.lookback_days"
_PANEL_SECTION = "universe."


def override(cfg: AppConfig, path: str, value: object) -> AppConfig:
    """返回把 path（形如 strategy.min_dividend_yield）改为 value 的新配置，原配置不变。

    新配置重新经过 pydantic 校验：路径未知、类型不符或取值不在允许范围内都抛 ValueError。
    """
    section, dot, field = path.partition(".")
    raw = cast("dict[str, object]", cfg.model_dump())
    body = raw.get(section)
    if not dot or not isinstance(body, dict) or field not in body:
        raise ValueError(
            f"未知的参数路径 {path!r}，应写成 区段.字段，如 strategy.min_dividend_yield"
        )
    cast("dict[str, object]", body)[field] = value
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        reason = exc.errors()[0]["msg"]
        raise ValueError(f"参数 {path} 的取值 {value!r} 无效：{reason}") from exc


def parse_param(cfg: AppConfig, text: str) -> tuple[str, list[object]]:
    """解析一条 --param，如 strategy.stop_loss=0.10,0.15；每个取值按目标字段的类型转换。"""
    path, eq, raw_values = text.partition("=")
    items = [item.strip() for item in raw_values.split(",")]
    if not path.strip() or not eq or not all(items):
        raise ValueError(f"--param 格式应为 路径=值1,值2,...（收到 {text!r}）")
    section, _, field = path.strip().partition(".")
    values: list[object] = []
    for item in items:
        new = override(cfg, path.strip(), item)
        values.append(cast("object", getattr(getattr(new, section), field)))
    return path.strip(), values


def _rebuilds_panel(paths: Sequence[str]) -> bool:
    return any(p == _PANEL_PARAM or p.startswith(_PANEL_SECTION) for p in paths)


def run_sweep(
    cfg: AppConfig,
    store: ParquetStore,
    grid: Mapping[str, Sequence[object]],
    progress: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """对 grid 的笛卡尔积逐一回测，返回每组参数一行的表。

    顺序确定：参数按 grid 的顺序，取值按给定顺序，后面的参数变化最快。
    列为每个参数（列名为完整路径），然后是 annual_return、max_drawdown、sharpe、win_rate、
    trade_count。所有组合先校验再运行，任何一组无效都不会开始回测。
    指标面板只建一次并复用；若扫描 universe.* 或 strategy.lookback_days 则每组重建。
    """
    if not grid:
        raise ValueError("至少需要扫描一个参数")
    for path, values in grid.items():
        if path in _SAMPLE_BOUNDARIES:
            raise ValueError(f"不能扫描 {path}：样本内外的分界线不允许在参数对比中改动")
        if len(values) == 0:
            raise ValueError(f"参数 {path} 没有取值")
    paths = list(grid)
    combos = list(itertools.product(*grid.values()))
    configs: list[AppConfig] = []
    for combo in combos:
        one = cfg
        for path, value in zip(paths, combo, strict=True):
            one = override(one, path, value)
        configs.append(one)

    start: date = cfg.backtest.in_sample_start
    end: date = cfg.backtest.in_sample_end
    last = last_bar_day(store)
    panel: FeaturePanel | None = None
    if not _rebuilds_panel(paths):
        calendar, first, final = clamp_period(store, start, end, last)
        panel = build_panel(store, calendar, cfg, first, final)

    rows: list[dict[str, object]] = []
    for done, (combo, one) in enumerate(zip(combos, configs, strict=True), start=1):
        result = run_period(one, store, start, end, last_day=last, panel=panel)
        m = compute_metrics(result.equity, result.trades)
        rows.append(
            {
                **dict(zip(paths, combo, strict=True)),
                "annual_return": m.annual_return,
                "max_drawdown": m.max_drawdown,
                "sharpe": m.sharpe,
                "win_rate": m.win_rate,
                "trade_count": m.trade_count,
            }
        )
        if progress is not None:
            progress(done, len(configs))
    return pd.DataFrame(rows, columns=[*paths, *RESULT_COLUMNS])
