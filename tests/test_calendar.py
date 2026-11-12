from datetime import date

import pytest

from qt.calendar import TradingCalendar


def test_calendar_offset_and_count() -> None:
    cal = TradingCalendar([date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 8)])
    assert cal.offset(date(2024, 1, 4), 1) == date(2024, 1, 8)
    assert cal.offset(date(2024, 1, 4), -2) == date(2024, 1, 2)
    assert cal.count_between(date(2024, 1, 2), date(2024, 1, 8)) == 3
    assert cal.between(date(2024, 1, 3), date(2024, 1, 8)) == [
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 8),
    ]
    assert not cal.is_trading_day(date(2024, 1, 6))
    with pytest.raises(ValueError):
        cal.offset(date(2024, 1, 8), 1)
