"""数据表定义与校验。日期列统一为 datetime64[ns]，symbol 为 6 位字符串。"""

from enum import StrEnum

import pandas as pd

DATE = "datetime64[ns]"


class SchemaError(ValueError):
    """数据表的列或类型不符合定义。"""


class Table(StrEnum):
    """数据表名。"""

    TRADE_DAYS = "trade_days"
    SECURITIES = "securities"
    BARS = "bars"
    DIVIDENDS = "dividends"
    FINANCIALS = "financials"
    INDUSTRIES = "industries"
    ST_HISTORY = "st_history"
    BENCHMARKS = "benchmarks"


# 列名 -> 类型；类型取值 "str"、"datetime64[ns]"、"bool"、"int64"、"float64"；字典顺序即列顺序
SCHEMAS: dict[Table, dict[str, str]] = {
    Table.TRADE_DAYS: {"day": DATE},
    Table.SECURITIES: {
        "symbol": "str",
        "name": "str",
        "board": "str",
        "list_date": DATE,
        "delist_date": DATE,  # 可空，未退市为 NaT
    },
    Table.BARS: {
        "symbol": "str",
        "day": DATE,
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
        "amount": "float64",
        "adj_factor": "float64",
    },
    Table.DIVIDENDS: {
        "symbol": "str",
        "fiscal_year": "int64",
        "announce_date": DATE,
        "ex_date": DATE,
        "cash_per_share": "float64",  # 每股税前现金分红（元，按除权前股本）
        "bonus_per_share": "float64",  # 每股送转股数：(送股 + 转增) / 10，没有为 0.0
    },
    Table.FINANCIALS: {
        "symbol": "str",
        "report_period": DATE,
        "announce_date": DATE,
        "roe": "float64",
        "net_profit": "float64",
        "net_profit_yoy": "float64",
        "debt_ratio": "float64",
        "eps": "float64",
    },
    Table.INDUSTRIES: {"symbol": "str", "industry": "str", "is_financial": "bool"},
    Table.ST_HISTORY: {"symbol": "str", "start": DATE, "end": DATE},  # end 可空
    Table.BENCHMARKS: {"code": "str", "day": DATE, "close": "float64"},
}

# 按股票分文件存储的表及其主键
PRIMARY_KEYS: dict[Table, list[str]] = {
    Table.TRADE_DAYS: ["day"],
    Table.SECURITIES: ["symbol"],
    Table.BARS: ["symbol", "day"],
    Table.DIVIDENDS: ["symbol", "ex_date"],
    Table.FINANCIALS: ["symbol", "report_period"],
    Table.INDUSTRIES: ["symbol"],
    Table.ST_HISTORY: ["symbol", "start"],
    Table.BENCHMARKS: ["code", "day"],
}

PER_SYMBOL_TABLES: frozenset[Table] = frozenset({Table.BARS, Table.DIVIDENDS, Table.FINANCIALS})


def empty_frame(table: Table) -> pd.DataFrame:
    """返回列与类型都正确的空表。"""
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in SCHEMAS[table].items()})


def _dtype_ok(series: pd.Series, expected: str) -> bool:
    if expected == "str":
        return bool(pd.api.types.is_string_dtype(series.dtype))
    return str(series.dtype) == expected


def validate(table: Table, df: pd.DataFrame) -> None:
    """校验列集合与类型，缺列、多列、类型不符抛 SchemaError。"""
    schema = SCHEMAS[table]
    cols = [str(c) for c in df.columns]
    missing = [c for c in schema if c not in cols]
    extra = [c for c in cols if c not in schema]
    if missing:
        raise SchemaError(f"{table.value} 缺少列：{missing}")
    if extra:
        raise SchemaError(f"{table.value} 多出列：{extra}")
    for col, expected in schema.items():
        series: pd.Series = df[col]
        if not _dtype_ok(series, expected):
            raise SchemaError(f"{table.value}.{col} 类型应为 {expected}，实际为 {series.dtype}")
