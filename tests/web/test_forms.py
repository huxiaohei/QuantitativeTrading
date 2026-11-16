"""表单解析：只判断格式，格式不对抛 FormError（带字段名与中文原因）。"""

from datetime import date

import pytest

from qt.web.forms import (
    FormError,
    parse_bool,
    parse_date,
    parse_money,
    parse_password,
    parse_shares,
)


def test_parse_date() -> None:
    assert parse_date(" 2026-11-16 ", "day") == date(2026, 11, 16)


@pytest.mark.parametrize("text", ["", "2026/11/16", "2026-13-01", "明天", "20261116"])
def test_parse_date_bad(text: str) -> None:
    with pytest.raises(FormError) as info:
        parse_date(text, "day")
    assert info.value.field == "day"
    assert "日期" in info.value.message


@pytest.mark.parametrize(
    ("text", "expected"),
    [("100", 100.0), (" 12.50 ", 12.5), ("-3.2", -3.2), ("0", 0.0), ("+7", 7.0)],
)
def test_parse_money(text: str, expected: float) -> None:
    assert parse_money(text, "cash") == expected


@pytest.mark.parametrize("text", ["", "abc", "1e5", "nan", "inf", "1,000", "1.2.3", "."])
def test_parse_money_bad(text: str) -> None:
    with pytest.raises(FormError) as info:
        parse_money(text, "cash")
    assert info.value.field == "cash"
    assert "金额" in info.value.message


def test_parse_shares() -> None:
    assert parse_shares("1000", "shares") == 1000.0
    assert parse_shares("100.5", "shares") == 100.5


@pytest.mark.parametrize("text", ["", "一百", "1e3", "-"])
def test_parse_shares_bad(text: str) -> None:
    with pytest.raises(FormError) as info:
        parse_shares(text, "shares")
    assert "数量" in info.value.message


@pytest.mark.parametrize(
    ("text", "expected"),
    [(None, False), ("", False), ("on", True), ("true", True), ("1", True), ("off", False)],
)
def test_parse_bool(text: str | None, expected: bool) -> None:
    assert parse_bool(text) is expected


def test_parse_password_length_cap() -> None:
    assert parse_password("x" * 1024, "password") == "x" * 1024
    with pytest.raises(FormError) as info:
        parse_password("x" * 1025, "password")
    assert info.value.message == "密码不能超过 1024 个字符"
