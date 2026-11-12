"""配置模型与加载。所有数值参数来自 config/default.yaml。"""

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    """不可变、禁止未知键的配置基类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UniverseConfig(_Frozen):
    """股票池：板块与流动性、上市年限过滤。"""

    boards: list[Literal["sh_main", "sz_main", "chinext"]]
    min_listed_years: int
    min_avg_amount_60d: float


class StrategyConfig(_Frozen):
    """高股息低位策略的选股与卖出参数。"""

    min_dividend_yield: float
    max_price_position: float
    lookback_days: int
    min_dividend_years: int
    min_roe_avg3: float
    max_payout_ratio: float
    min_profit_yoy: float
    max_debt_ratio: float
    take_profit_mode: Literal["fixed", "trailing"]
    take_profit_fixed: float
    trailing_activate: float
    trailing_drawdown: float
    stop_loss: float
    time_stop_days: int
    fundamental_profit_yoy: float


class RiskConfig(_Frozen):
    """仓位与回撤风控参数。"""

    max_position_pct: float
    max_positions: int
    max_industry_pct: float
    warn_drawdown: float
    pause_drawdown: float
    pause_days: int
    cooldown_days: int


class CostsConfig(_Frozen):
    """交易成本参数。"""

    commission_rate: float
    min_commission: float
    stamp_tax_rate: float
    transfer_fee_rate: float
    slippage: float


class BacktestConfig(_Frozen):
    """回测区间、本金与基准。"""

    initial_cash: float
    data_start: date
    in_sample_start: date
    in_sample_end: date
    out_sample_start: date
    buy_order_valid_days: int
    benchmarks: list[str]


class DataConfig(_Frozen):
    """数据下载参数：基本面刷新间隔、请求间隔与重试次数。"""

    fundamentals_refresh_days: int
    request_pause_seconds: float
    retries: int


class AppConfig(_Frozen):
    """应用总配置。"""

    universe: UniverseConfig
    strategy: StrategyConfig
    risk: RiskConfig
    costs: CostsConfig
    backtest: BacktestConfig
    data: DataConfig


def load_config(path: Path) -> AppConfig:
    """从 YAML 文件加载并校验配置。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)
