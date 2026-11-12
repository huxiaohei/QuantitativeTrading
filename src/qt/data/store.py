"""Parquet 存储。

按股票分文件的表放 {root}/{table}/{symbol}.parquet，其余放 {root}/{table}.parquet。
"""

import os
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd

from qt.data.schema import (
    PER_SYMBOL_TABLES,
    PRIMARY_KEYS,
    SCHEMAS,
    Table,
    empty_frame,
    validate,
)


def _as_type(series: pd.Series, dtype: str) -> pd.Series:
    """把列转成指定类型（pandas-stubs 不接受字符串 dtype，这里集中处理）。"""
    return cast(pd.Series, series.astype(cast(Any, dtype)))


def _normalize(table: Table, df: pd.DataFrame) -> pd.DataFrame:
    """按表定义调整列顺序与类型，并按主键排序。"""
    schema = SCHEMAS[table]
    cols: dict[str, pd.Series] = {c: _as_type(df[c], t) for c, t in schema.items()}
    out = pd.DataFrame(cols)
    return out.sort_values(PRIMARY_KEYS[table]).reset_index(drop=True)


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    """先写同目录临时文件再换名；失败时清理临时文件并重新抛出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    os.close(fd)
    try:
        df.to_parquet(tmp_name, index=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _query(sql: str, params: list[object]) -> pd.DataFrame:
    """用 DuckDB 执行查询并返回 DataFrame。"""
    con = duckdb.connect()
    try:
        return con.execute(sql, params).df()
    finally:
        con.close()


class ParquetStore:
    """基于 Parquet 文件的数据存储。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, table: Table, symbol: str | None) -> Path:
        if table in PER_SYMBOL_TABLES:
            if symbol is None:
                raise ValueError(f"{table.value} 按股票分文件，必须指定 symbol")
            if not symbol.isalnum():
                raise ValueError(f"非法 symbol：{symbol!r}")
            return self._root / table.value / f"{symbol}.parquet"
        if symbol is not None:
            raise ValueError(f"{table.value} 不按股票分文件，不能指定 symbol")
        return self._root / f"{table.value}.parquet"

    def write(self, table: Table, df: pd.DataFrame, symbol: str | None = None) -> None:
        """整表覆盖写入。"""
        validate(table, df)
        _write_atomic(self._path(table, symbol), _normalize(table, df))

    def append(self, table: Table, df: pd.DataFrame, symbol: str) -> None:
        """追加并按主键去重，后写入的行覆盖旧行。"""
        validate(table, df)
        path = self._path(table, symbol)
        if path.exists():
            old = _normalize(table, pd.read_parquet(path))
            df = pd.concat([old, _normalize(table, df)], ignore_index=True)
        df = df.drop_duplicates(subset=PRIMARY_KEYS[table], keep="last")
        _write_atomic(path, _normalize(table, df))

    def read(
        self,
        table: Table,
        symbols: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """读取表；start/end 作用于 day 列（闭区间），无 day 列的表忽略。"""
        if table in PER_SYMBOL_TABLES and symbols is None:
            # 对 glob 路径查询；目录不存在时 DuckDB 会报错，故先判断
            if not self.has(table):
                return empty_frame(table)
            source: str | list[str] = str(self._root / table.value / "*.parquet")
        elif table in PER_SYMBOL_TABLES:
            files = [str(p) for p in (self._path(table, s) for s in symbols or []) if p.exists()]
            if not files:
                return empty_frame(table)
            source = files
        else:
            path = self._path(table, None)
            if not path.exists():
                return empty_frame(table)
            source = str(path)

        params: list[object] = [source]
        conds: list[str] = []
        if "day" in SCHEMAS[table]:
            if start is not None:
                conds.append("day >= ?")
                params.append(start)
            if end is not None:
                conds.append("day <= ?")
                params.append(end)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        df = _query(f"SELECT * FROM read_parquet(?){where}", params)
        return _normalize(table, df)

    def last_day(self, table: Table, symbol: str) -> date | None:
        """某只股票在该表中的最大 day；没有数据返回 None。"""
        if "day" not in SCHEMAS[table]:
            raise ValueError(f"{table.value} 没有 day 列")
        path = self._path(table, symbol)
        if not path.exists():
            return None
        df = _query("SELECT max(day) AS d FROM read_parquet(?)", [str(path)])
        value = cast(pd.Timestamp, df["d"].iloc[0])
        if pd.isna(value):
            return None
        return value.date()

    def has(self, table: Table, symbol: str | None = None) -> bool:
        """表（或某只股票的文件）是否已存在。"""
        if table in PER_SYMBOL_TABLES and symbol is None:
            directory = self._root / table.value
            return directory.is_dir() and any(directory.glob("*.parquet"))
        return self._path(table, symbol).exists()
