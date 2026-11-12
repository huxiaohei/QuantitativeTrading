"""数据表定义测试。"""

from datetime import date

import pytest

from qt.data.schema import SCHEMAS, SchemaError, Table, empty_frame, validate
from tests.fakes import make_bars


def test_validate_accepts_good_bars() -> None:
    validate(Table.BARS, make_bars("600000", [date(2024, 1, 2)], [10.0]))


def test_validate_accepts_empty_frame_for_every_table() -> None:
    for table in Table:
        validate(table, empty_frame(table))


def test_validate_rejects_missing_column() -> None:
    df = make_bars("600000", [date(2024, 1, 2)], [10.0]).drop(columns=["adj_factor"])
    with pytest.raises(SchemaError):
        validate(Table.BARS, df)


def test_validate_rejects_extra_column() -> None:
    df = make_bars("600000", [date(2024, 1, 2)], [10.0])
    df["extra"] = 1.0
    with pytest.raises(SchemaError):
        validate(Table.BARS, df)


def test_validate_rejects_wrong_dtype() -> None:
    df = make_bars("600000", [date(2024, 1, 2)], [10.0])
    df["volume"] = df["volume"].astype("int64")
    with pytest.raises(SchemaError):
        validate(Table.BARS, df)


def test_every_table_has_schema() -> None:
    assert set(SCHEMAS) == set(Table)
