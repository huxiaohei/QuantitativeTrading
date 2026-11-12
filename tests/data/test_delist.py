"""退市日期定稿：以已存储日线为准。"""

from datetime import date
from pathlib import Path

import pandas as pd

from qt.data.delist import UNKNOWN_DELIST_DATE, finalize_delist_dates
from qt.data.schema import Table
from qt.data.store import ParquetStore
from tests.fakes import make_bars, make_securities, populate_store, weekdays

DAYS = weekdays(date(2024, 1, 1), 10)
LISTED = date(2000, 1, 3)


def _delist_dates(store: ParquetStore) -> dict[str, pd.Timestamp]:
    secs = store.read(Table.SECURITIES)
    return dict(zip(secs["symbol"], secs["delist_date"], strict=True))


def test_delist_date_is_day_after_last_bar_in_every_case(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    after_last = pd.Timestamp(DAYS[5]) + pd.Timedelta(days=1)
    populate_store(
        store,
        securities=make_securities(
            [
                ("600000", "sh_main", LISTED, None),  # 在市：不动
                ("600001", "sh_main", LISTED, DAYS[1]),  # 表中日期早于最后一根日线
                ("000004", "sz_main", LISTED, date(2024, 3, 1)),  # 表中日期晚于最后一根日线
                ("600002", "sh_main", LISTED, UNKNOWN_DELIST_DATE.date()),  # 表中没有日期
            ]
        ),
        bars=[make_bars(s, DAYS[:6], [3.0] * 6) for s in ("600001", "000004", "600002")]
        + [make_bars("600000", DAYS[:6], [3.0] * 6)],
    )
    dropped = finalize_delist_dates(store)
    dates = _delist_dates(store)
    assert dropped == []
    assert pd.isna(dates["600000"])
    assert dates["600001"] == after_last
    assert dates["000004"] == after_last
    assert dates["600002"] == after_last


def test_delist_date_without_bars(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(
        store,
        securities=make_securities(
            [
                ("600001", "sh_main", LISTED, date(2020, 5, 1)),  # 没有日线：保留表中日期
                ("600002", "sh_main", LISTED, UNKNOWN_DELIST_DATE.date()),  # 没有日线也没有日期
            ]
        ),
    )
    dropped = finalize_delist_dates(store)
    assert dropped == ["600002"]
    assert _delist_dates(store) == {"600001": pd.Timestamp(2020, 5, 1)}


def test_finalize_is_idempotent(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    populate_store(
        store,
        securities=make_securities([("600001", "sh_main", LISTED, DAYS[1])]),
        bars=[make_bars("600001", DAYS[:6], [3.0] * 6)],
    )
    finalize_delist_dates(store)
    first = _delist_dates(store)
    finalize_delist_dates(store)
    assert _delist_dates(store) == first
