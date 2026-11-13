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
    assert cfg.strategy.take_profit_fixed == 0.20
    assert cfg.risk.pause_drawdown == 0.20
    assert cfg.risk.cooldown_days == 60
    assert cfg.costs.min_commission == 5.0
    assert [(p.start.isoformat(), p.rate) for p in cfg.costs.stamp_tax_schedule] == [
        ("2008-09-19", 0.001),
        ("2023-08-28", 0.0005),
    ]
    assert cfg.universe.boards == ["sh_main", "sz_main"]
    assert cfg.backtest.initial_cash == 150000
    assert cfg.backtest.data_start.isoformat() == "2009-01-01"
    assert cfg.backtest.benchmarks == ["H00300", "510880"]
    assert cfg.data.fundamentals_refresh_days == 7
    assert cfg.data.request_pause_seconds == 1.0
    assert cfg.data.retries == 3
    assert cfg.data.industries_refresh_days == 30
    assert cfg.data.min_industry_coverage == 0.9


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


def test_chinext_board_rejected(tmp_path: Path) -> None:
    path = _write_modified(tmp_path, "universe", "boards", ["sh_main", "chinext"])
    with pytest.raises(ValidationError):
        load_config(path)


def test_empty_or_non_mapping_yaml_rejected_with_chinese_message(tmp_path: Path) -> None:
    for text in ("", "- a\n- b\n", "42\n"):
        path = tmp_path / "cfg.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="配置文件"):
            load_config(path)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("strategy", "stop_loss", -0.1),
        ("strategy", "stop_loss", float("nan")),
        ("strategy", "min_dividend_yield", float("inf")),
        ("strategy", "max_price_position", 1.5),
        ("strategy", "lookback_days", 0),
        ("strategy", "time_stop_days", -5),
        ("risk", "max_position_pct", 0.0),
        ("risk", "max_position_pct", 1.2),
        ("risk", "max_positions", 0),
        ("risk", "pause_drawdown", 1.0),
        ("risk", "cooldown_days", -1),
        ("costs", "commission_rate", -0.001),
        ("costs", "slippage", 1.0),
        ("backtest", "initial_cash", 0),
        ("backtest", "buy_order_valid_days", 0),
        ("data", "retries", 0),
        ("data", "min_industry_coverage", 1.5),
        ("universe", "boards", []),
    ],
)
def test_out_of_range_values_rejected(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    path = _write_modified(tmp_path, section, key, value)
    with pytest.raises(ValidationError):
        load_config(path)


def test_zero_cooldown_allowed(tmp_path: Path) -> None:
    path = _write_modified(tmp_path, "risk", "cooldown_days", 0)
    assert load_config(path).risk.cooldown_days == 0


def test_sample_boundaries_must_be_ordered(tmp_path: Path) -> None:
    for key, value in (
        ("in_sample_end", "2014-12-31"),  # 早于 in_sample_start
        ("out_sample_start", "2021-12-31"),  # 不晚于 in_sample_end
    ):
        path = _write_modified(tmp_path, "backtest", key, value)
        with pytest.raises(ValidationError):
            load_config(path)


def test_stamp_tax_schedule_must_be_sorted(tmp_path: Path) -> None:
    unsorted = [
        {"start": "2023-08-28", "rate": 0.0005},
        {"start": "2008-09-19", "rate": 0.001},
    ]
    for schedule in (unsorted, []):
        path = _write_modified(tmp_path, "costs", "stamp_tax_schedule", schedule)
        with pytest.raises(ValidationError):
            load_config(path)
