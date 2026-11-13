"""Parquet 存储。

按股票分文件的表放 {root}/{table}/{symbol}.parquet，其余放 {root}/{table}.parquet。
"""

import os
import tempfile
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd

from qt.data.schema import (
    PER_SYMBOL_TABLES,
    PRIMARY_KEYS,
    SCHEMAS,
    SchemaError,
    Table,
    empty_frame,
    validate,
)


class StoredSchemaError(SchemaError):
    """已存储的文件缺少当前表定义里的列（由旧版本写入），需要删除后重新下载。"""


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
        try:
            df = _query(f"SELECT * FROM read_parquet(?){where}", params)
        except duckdb.Error:
            # 多个文件列不一致时 DuckDB 报错；若是旧格式文件，换成说明清楚的错误
            self._raise_if_outdated(table, source)
            raise
        if any(c not in df.columns for c in SCHEMAS[table]):
            self._raise_if_outdated(table, source)
        return _normalize(table, df)

    def _raise_if_outdated(self, table: Table, source: str | list[str]) -> None:
        """source 中有文件缺少表定义里的列时抛 StoredSchemaError（中文说明与处理办法）。"""
        listing = _query("SELECT file_name, name FROM parquet_schema(?)", [source])
        required = list(SCHEMAS[table])
        have: dict[str, set[str]] = {}
        for file_name, name in zip(listing["file_name"], listing["name"], strict=True):
            have.setdefault(str(file_name), set()).add(str(name))
        bad = sorted(f for f, cols in have.items() if any(c not in cols for c in required))
        if not bad:
            return
        missing = sorted({c for f in bad for c in required if c not in have[f]})
        if table in PER_SYMBOL_TABLES:
            where = self._root / table.value
            remedy = f"请删除该目录后重新运行 qt data update（目录：{where}）"
        else:
            where = self._path(table, None)
            remedy = f"请删除该文件后重新运行 qt data update（文件：{where}）"
        raise StoredSchemaError(
            f"{table.value} 表有 {len(bad)} 个已存储文件缺少列 {missing}"
            f"（由旧版本写入，如 {Path(bad[0]).name}）。{remedy}"
        )

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

    def modified_time(self, table: Table, symbol: str | None = None) -> datetime | None:
        """文件的最后修改时间（本地时间）；文件不存在返回 None。"""
        path = self._path(table, symbol)
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime)

    def set_modified_time(self, table: Table, symbol: str | None, when: datetime) -> None:
        """把文件的修改时间设为 when（用于部分更新后保留原有的“新旧程度”）。"""
        path = self._path(table, symbol)
        os.utime(path, (when.timestamp(), when.timestamp()))
