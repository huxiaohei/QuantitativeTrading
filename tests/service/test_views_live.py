"""持仓按盘中报价重算（只用于显示）。"""

from datetime import datetime

import pytest

from qt.data.quotes import LiveQuote
from qt.service import views

T1 = datetime(2026, 11, 17, 10, 0, 3)
T2 = datetime(2026, 11, 17, 10, 0, 9)


def _row(symbol: str, close: float | None = 10.0) -> views.HoldingRow:
    """1000 股、成本 10100（含费用）、已收现金分红 200。"""
    value = None if close is None else 1000 * close
    return views.HoldingRow(
        symbol=symbol,
        name="名称",
        shares=1000,
        cost=10100.0,
        cost_price=10.1,
        close=close,
        value=value,
        ret=None if value is None else (value + 200) / 10100 - 1,
        pnl=None if value is None else value + 200 - 10100,
        origin="signal",
        origin_label="信号",
        stop_price=9.0,
        take_price=None,
        cash_received=200.0,
    )


def test_live_price_recomputes_value_pnl_and_return_with_dividends() -> None:
    live = views.apply_quotes([_row("600519")], {"600519": LiveQuote(11.0, 10.0, T1)})
    row = live.rows[0]
    assert (row.close, row.value, row.price_source) == (11.0, 11000.0, "live")
    assert row.pnl == pytest.approx(11000 + 200 - 10100)
    assert row.ret == pytest.approx((11000 + 200) / 10100 - 1)
    # 只改显示用的价格：成本、止损位等原样
    assert (row.cost, row.cost_price, row.stop_price, row.shares) == (10100.0, 10.1, 9.0, 1000)
    assert (live.live_count, live.suspended_count, live.close_count) == (1, 0, 0)
    assert live.quote_time == T1


def test_zero_price_uses_prev_close_and_is_marked_suspended() -> None:
    live = views.apply_quotes([_row("600519", close=9.8)], {"600519": LiveQuote(0.0, 9.9, T1)})
    row = live.rows[0]
    assert (row.close, row.value, row.price_source) == (9.9, 9900.0, "suspended")
    assert (live.live_count, live.suspended_count, live.close_count) == (0, 1, 0)


def test_missing_or_unusable_quote_keeps_the_close() -> None:
    rows = [_row("600519"), _row("000001"), _row("000002", close=None)]
    quotes = {"000001": LiveQuote(0.0, 0.0, T1)}
    live = views.apply_quotes(rows, quotes)
    assert live.rows == rows
    assert (live.live_count, live.suspended_count, live.close_count) == (0, 0, 3)
    assert live.quote_time is None


def test_quote_fills_a_row_that_had_no_close_and_time_is_the_newest() -> None:
    rows = [_row("600519", close=None), _row("000001")]
    quotes = {"600519": LiveQuote(12.0, 11.0, T1), "000001": LiveQuote(10.5, 10.0, T2)}
    live = views.apply_quotes(rows, quotes)
    assert live.rows[0].value == 12000.0
    assert live.rows[0].pnl == pytest.approx(12000 + 200 - 10100)
    assert live.quote_time == T2
