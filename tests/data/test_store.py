"""Parquet 存储测试。"""

import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qt.data.schema import SchemaError, Table, empty_frame
from qt.data.store import ParquetStore, StoredSchemaError
from tests.fakes import make_bars, make_dividends

D = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]


def test_roundtrip_and_filter(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write(Table.BARS, make_bars("600000", D[:3], [1.0, 2.0, 3.0]), symbol="600000")
    store.write(Table.BARS, make_bars("000001", D[:3], [4.0, 5.0, 6.0]), symbol="000001")
    df = store.read(Table.BARS, symbols=["600000"], start=D[1])
    assert len(df) == 2
    assert df["symbol"].tolist() == ["600000", "600000"]
    assert df["close"].tolist() == [2.0, 3.0]
    both = store.read(Table.BARS)
    assert len(both) == 6
    assert both["symbol"].tolist() == ["000001"] * 3 + ["600000"] * 3
    assert str(both["day"].dtype) == "datetime64[ns]"
    only_end = store.read(Table.BARS, symbols=["000001"], end=D[1])
    assert only_end["close"].tolist() == [4.0, 5.0]


def test_append_dedupes_on_primary_key(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write(Table.BARS, make_bars("600000", D[:3], [1.0, 2.0, 3.0]), symbol="600000")
    store.append(Table.BARS, make_bars("600000", D[2:], [30.0, 4.0]), symbol="600000")
    df = store.read(Table.BARS, symbols=["600000"])
    assert len(df) == 4
    assert df["close"].tolist() == [1.0, 2.0, 30.0, 4.0]


def test_append_creates_when_missing(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.append(Table.BARS, make_bars("600000", D[:2], [1.0, 2.0]), symbol="600000")
    assert len(store.read(Table.BARS)) == 2


def test_last_day_and_has(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    assert store.last_day(Table.BARS, "600000") is None
    assert not store.has(Table.BARS, "600000")
    assert not store.has(Table.BARS)
    store.write(Table.BARS, make_bars("600000", D[:3], [1.0, 2.0, 3.0]), symbol="600000")
    assert store.last_day(Table.BARS, "600000") == D[2]
    assert store.has(Table.BARS, "600000")
    assert store.has(Table.BARS)
    assert not store.has(Table.BARS, "000001")


def test_read_empty_is_typed(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    df = store.read(Table.BARS)
    assert df.empty
    assert list(df.columns) == list(empty_frame(Table.BARS).columns)
    assert df.dtypes.to_dict() == empty_frame(Table.BARS).dtypes.to_dict()


def test_non_symbol_table_and_nullable_dates(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    df = pd.DataFrame(
        {
            "symbol": ["600000", "000001"],
            "name": ["浦发银行", "平安银行"],
            "board": ["main", "main"],
            "list_date": pd.Series(
                [pd.Timestamp("1999-11-10"), pd.Timestamp("1991-04-03")], dtype="datetime64[ns]"
            ),
            "delist_date": pd.Series([pd.NaT, pd.Timestamp("2030-01-01")], dtype="datetime64[ns]"),
        }
    )
    store.write(Table.SECURITIES, df)
    assert (tmp_path / "securities.parquet").exists()
    out = store.read(Table.SECURITIES)
    assert out["symbol"].tolist() == ["000001", "600000"]
    assert out["delist_date"].isna().tolist() == [False, True]
    assert str(out["delist_date"].dtype) == "datetime64[ns]"
    assert store.has(Table.SECURITIES)


def test_write_validates(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bad = make_bars("600000", D[:1], [1.0]).drop(columns=["amount"])
    with pytest.raises(SchemaError):
        store.write(Table.BARS, bad, symbol="600000")
    with pytest.raises(SchemaError):
        store.append(Table.BARS, bad, symbol="600000")


def test_interrupted_write_leaves_old_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ParquetStore(tmp_path)
    store.write(Table.BARS, make_bars("600000", D[:2], [1.0, 2.0]), symbol="600000")

    def boom(src: object, dst: object) -> None:
        raise OSError("模拟换名失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write(Table.BARS, make_bars("600000", D[:3], [9.0, 9.0, 9.0]), symbol="600000")
    monkeypatch.undo()
    assert store.read(Table.BARS)["close"].tolist() == [1.0, 2.0]
    assert [p.name for p in (tmp_path / "bars").iterdir()] == ["600000.parquet"]


def test_modified_time(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    assert store.modified_time(Table.DIVIDENDS, "600000") is None
    store.write(Table.BARS, make_bars("600000", D[:2], [1.0, 2.0]), symbol="600000")
    path = tmp_path / "bars" / "600000.parquet"
    os.utime(path, (1_700_000_000, 1_700_000_000))
    stamp = store.modified_time(Table.BARS, "600000")
    assert stamp is not None
    assert stamp.timestamp() == 1_700_000_000


# ---------------------------------------------------------------- 旧版本写入的文件


def _write_old_dividends(root: Path, symbol: str) -> None:
    """手写一个缺少 bonus_per_share 列的旧格式分红文件。"""
    old = make_dividends(symbol, [(2020, D[0], D[1], 0.3)]).drop(columns=["bonus_per_share"])
    (root / "dividends").mkdir(parents=True, exist_ok=True)
    old.to_parquet(root / "dividends" / f"{symbol}.parquet", index=False)


def _assert_outdated_message(exc: pytest.ExceptionInfo[StoredSchemaError]) -> None:
    text = str(exc.value)
    assert "dividends" in text
    assert "bonus_per_share" in text
    assert "删除该目录后重新运行 qt data update" in text


def test_reading_old_schema_file_raises_clear_chinese_error(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    _write_old_dividends(tmp_path, "600000")
    with pytest.raises(StoredSchemaError) as exc:
        store.read(Table.DIVIDENDS, symbols=["600000"])
    _assert_outdated_message(exc)
    with pytest.raises(StoredSchemaError) as exc:
        store.read(Table.DIVIDENDS)
    _assert_outdated_message(exc)


@pytest.mark.parametrize("order", [("000001", "600000"), ("600000", "000001")])
def test_mixed_old_and_new_files_raise_the_same_error(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    store = ParquetStore(tmp_path)
    store.write(Table.DIVIDENDS, make_dividends("000001", [(2020, D[0], D[1], 0.3)]), "000001")
    _write_old_dividends(tmp_path, "600000")
    with pytest.raises(StoredSchemaError) as exc:
        store.read(Table.DIVIDENDS, symbols=list(order))
    _assert_outdated_message(exc)
    assert "600000" in str(exc.value)
    with pytest.raises(StoredSchemaError) as exc:
        store.read(Table.DIVIDENDS)
    _assert_outdated_message(exc)
    assert isinstance(exc.value, SchemaError)
