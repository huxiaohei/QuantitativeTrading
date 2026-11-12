"""测试辅助：假数据与默认配置。"""

from pathlib import Path

from qt.config import CostsConfig, load_config

_ROOT = Path(__file__).resolve().parent.parent


def default_costs() -> CostsConfig:
    """返回 config/default.yaml 中的 costs 配置。"""
    return load_config(_ROOT / "config" / "default.yaml").costs
