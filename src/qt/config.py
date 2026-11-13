"""配置模型与加载。所有数值参数来自 config/default.yaml。

每个字段都有取值范围：比例类在 (0, 1] 或 [0, 1) 内，天数、个数为正整数（冷却期等允许 0 表示关闭），
任何浮点数都不接受 NaN 与无穷大。超出范围的配置（包括 qt backtest sweep 的 --param）一律报错。
"""

import itertools
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 常用取值范围
PositiveFraction = Annotated[float, Field(gt=0, le=1)]  # (0, 1]
OpenFraction = Annotated[float, Field(gt=0, lt=1)]  # (0, 1)
Rate = Annotated[float, Field(ge=0, lt=1)]  # [0, 1)
Share = Annotated[float, Field(ge=0, le=1)]  # [0, 1]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _Frozen(BaseModel):
    """不可变、禁止未知键、拒绝 NaN 与无穷大的配置基类。"""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class UniverseConfig(_Frozen):
    """股票池：板块与流动性、上市年限过滤。

    只支持沪市主板与深市主板：涨跌停按 ±10%（ST ±5%）建模，创业板（±20%）等不适用。
    """

    boards: Annotated[list[Literal["sh_main", "sz_main"]], Field(min_length=1)]
    min_listed_years: NonNegativeInt
    min_avg_amount_60d: NonNegativeFloat


class StrategyConfig(_Frozen):
    """高股息低位策略的选股与卖出参数。"""

    min_dividend_yield: Rate
    max_price_position: Share
    lookback_days: PositiveInt
    min_dividend_years: NonNegativeInt
    min_roe_avg3: float
    max_payout_ratio: PositiveFloat
    min_profit_yoy: float
    max_debt_ratio: PositiveFraction
    take_profit_mode: Literal["fixed", "trailing"]
    take_profit_fixed: PositiveFloat
    trailing_activate: PositiveFloat
    trailing_drawdown: OpenFraction
    stop_loss: OpenFraction
    time_stop_days: PositiveInt
    fundamental_profit_yoy: float


class RiskConfig(_Frozen):
    """仓位与回撤风控参数。"""

    max_position_pct: PositiveFraction
    max_positions: PositiveInt
    max_industry_pct: PositiveFraction
    warn_drawdown: OpenFraction
    pause_drawdown: OpenFraction
    pause_days: PositiveInt
    cooldown_days: NonNegativeInt  # 0 表示不冷却


class StampTaxPeriod(_Frozen):
    """印花税的一段：自 start（含）起按 rate 征收。"""

    start: date
    rate: Rate


class CostsConfig(_Frozen):
    """交易成本参数。印花税按历史税率分段，stamp_tax_schedule 按起始日升序排列。"""

    commission_rate: Rate
    min_commission: NonNegativeFloat
    stamp_tax_schedule: Annotated[list[StampTaxPeriod], Field(min_length=1)]
    transfer_fee_rate: Rate
    slippage: Rate

    @field_validator("stamp_tax_schedule")
    @classmethod
    def _ascending(cls, value: list[StampTaxPeriod]) -> list[StampTaxPeriod]:
        starts = [p.start for p in value]
        if any(a >= b for a, b in itertools.pairwise(starts)):
            raise ValueError("印花税分段必须按起始日严格升序排列")
        return value


class BacktestConfig(_Frozen):
    """回测区间、本金与基准。要求 in_sample_start < in_sample_end < out_sample_start。"""

    initial_cash: PositiveFloat
    data_start: date
    in_sample_start: date
    in_sample_end: date
    out_sample_start: date
    buy_order_valid_days: PositiveInt
    benchmarks: list[str]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.in_sample_start < self.in_sample_end < self.out_sample_start:
            raise ValueError("回测区间必须满足 in_sample_start < in_sample_end < out_sample_start")
        return self


class DataConfig(_Frozen):
    """数据下载参数：基本面刷新间隔、请求间隔与重试次数。"""

    fundamentals_refresh_days: NonNegativeInt
    request_pause_seconds: NonNegativeFloat
    retries: PositiveInt
    industries_refresh_days: NonNegativeInt
    min_industry_coverage: Share


class AppConfig(_Frozen):
    """应用总配置。"""

    universe: UniverseConfig
    strategy: StrategyConfig
    risk: RiskConfig
    costs: CostsConfig
    backtest: BacktestConfig
    data: DataConfig


def load_config(path: Path) -> AppConfig:
    """从 YAML 文件加载并校验配置；文件为空或顶层不是键值映射时抛 ValueError。"""
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件 {path} 为空或顶层不是键值映射，无法读取配置")
    return AppConfig.model_validate(raw)
