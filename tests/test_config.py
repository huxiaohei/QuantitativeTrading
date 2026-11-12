"""配置加载测试。"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from qt.config import load_config

DEFAULT = Path("config/default.yaml")


def _write_modified(tmp_path: Path, section: str, key: str, value: object) -> Path:
    raw = yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))
    raw[section][key] = value
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_default_config_matches_spec() -> None:
    cfg = load_config(DEFAULT)
    assert cfg.strategy.min_dividend_yield == 0.04
    assert cfg.strategy.take_profit_fixed == 0.12
    assert cfg.risk.pause_drawdown == 0.20
    assert cfg.risk.cooldown_days == 60
    assert cfg.costs.min_commission == 5.0
    assert cfg.universe.boards == ["sh_main", "sz_main"]
    assert cfg.backtest.initial_cash == 150000
    assert cfg.backtest.data_start.isoformat() == "2009-01-01"
    assert cfg.backtest.benchmarks == ["H00300", "510880"]
    assert cfg.data.fundamentals_refresh_days == 7
    assert cfg.data.request_pause_seconds == 1.0
    assert cfg.data.retries == 3


def test_unknown_key_rejected(tmp_path: Path) -> None:
    path = _write_modified(tmp_path, "strategy", "no_such_key", 1)
    with pytest.raises(ValidationError):
        load_config(path)


def test_unsupported_board_rejected(tmp_path: Path) -> None:
    path = _write_modified(tmp_path, "universe", "boards", ["star"])
    with pytest.raises(ValidationError):
        load_config(path)


def test_config_is_frozen() -> None:
    cfg = load_config(DEFAULT)
    with pytest.raises(ValidationError):
        cfg.risk.max_positions = 1
