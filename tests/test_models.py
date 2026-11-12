from datetime import date

import pytest

from qt.models import ExitReason, Position, Trade


def test_position_return_includes_reinvested_dividend() -> None:
    p = Position("600000", 1000, 10000.0, date(2020, 1, 2), "银行", {})
    p.scale_shares(1.05)  # 除权日因子上升 5%
    assert p.ret(9.6) == pytest.approx(0.008)  # 1050 * 9.6 / 10000 - 1


def test_trade_pnl_and_ret() -> None:
    t = Trade(
        "600000",
        date(2020, 1, 2),
        date(2020, 2, 3),
        10000.0,
        11000.0,
        ExitReason.TAKE_PROFIT_FIXED,
        21,
        {},
    )
    assert t.pnl == pytest.approx(1000.0)
    assert t.ret == pytest.approx(0.1)
