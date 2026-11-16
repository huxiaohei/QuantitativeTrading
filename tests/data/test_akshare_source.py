"""AKShare 原始表 → 标准表 的纯函数转换测试（不联网，列名取自 docs/data-gaps.md）。"""

import math
import threading
from datetime import date

import pandas as pd
import pytest

from qt.data.akshare_convert import (
    board_of_code,
    build_etf_total_return,
    build_industries,
    classify_industry,
    fiscal_year_of,
    normalize_bars,
    normalize_csindex,
    normalize_daily_snapshot,
    normalize_dividends,
    normalize_financials,
    normalize_securities,
    normalize_st_history,
    parse_amount,
    parse_disclosure,
    parse_percent,
    parse_plain,
    period_label,
    statutory_deadline,
)
from qt.data.akshare_source import (
    AkshareSource,
    RequestTimeoutError,
    call_with_deadline,
    call_with_retry,
)
from qt.data.delist import UNKNOWN_DELIST_DATE
from qt.data.schema import SCHEMAS, SNAPSHOT_SCHEMA, Table, validate
from tests.fakes import default_app_config


def _events(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """hfq-factor 事件表：date 为 datetime64，hfq_factor 为字符串，且按日期倒序。"""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "hfq_factor": pd.Series([r[1] for r in rows], dtype="str"),
        }
    )


def _raw_daily(rows: list[tuple[date, float, float, float]]) -> pd.DataFrame:
    """新浪未复权日线：date 为 datetime.date，行内为 (日期, 收盘, 成交量, 成交额)。"""
    return pd.DataFrame(
        {
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[1] + 0.1 for r in rows],
            "low": [r[1] - 0.1 for r in rows],
            "close": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
            "amount": [r[3] for r in rows],
            "outstanding_share": [1e10] * len(rows),
            "turnover": [0.001] * len(rows),
        }
    )


# ---------------------------------------------------------------- bars


def test_normalize_bars_computes_adj_factor() -> None:
    raw = _raw_daily(
        [
            (date(2024, 7, 16), 8.00, 50_000_000.0, 4.0e8),
            (date(2024, 7, 17), 8.10, 40_000_000.0, 3.2e8),
            (date(2024, 7, 18), 7.80, 30_000_000.0, 2.4e8),  # 除息日
            (date(2024, 7, 19), 7.90, 30_000_000.0, 2.4e8),
        ]
    )
    events = _events([("2024-07-18", "16.1186"), ("2023-07-21", "15.5465"), ("1900-01-01", "1.0")])
    out = normalize_bars("600000", raw, events)
    validate(Table.BARS, out)
    assert list(out.columns) == list(SCHEMAS[Table.BARS])
    assert out["symbol"].tolist() == ["600000"] * 4
    assert out["adj_factor"].tolist() == [15.5465, 15.5465, 16.1186, 16.1186]
    assert out["close"].tolist() == [8.00, 8.10, 7.80, 7.90]
    # 数值单位不再换算：新浪成交量已是股，成交额是元
    assert out["volume"].iloc[0] == 50_000_000.0
    assert out["amount"].iloc[0] == 4.0e8
    # 后复权收盘价在除息日连续（7.80 * 16.1186 与 8.10 * 15.5465 相近）
    hfq = out["close"] * out["adj_factor"]
    assert abs(hfq.iloc[2] / hfq.iloc[1] - 1) < 0.05


def test_normalize_bars_before_first_event_uses_one_and_empty_input() -> None:
    raw = _raw_daily([(date(2024, 1, 2), 5.0, 1e6, 5e6)])
    out = normalize_bars("000001", raw, _events([("2024-06-01", "2.0")]))
    assert out["adj_factor"].tolist() == [1.0]
    assert len(normalize_bars("000001", raw.iloc[0:0], _events([("1900-01-01", "1.0")]))) == 0


# ---------------------------------------------------------------- dividends


def _raw_dividends(
    rows: list[tuple[str, float, str, str | None]],
    bonus: list[tuple[float, float]] | None = None,
) -> pd.DataFrame:
    """新浪分红表；行内为 (公告日期, 派息, 进度, 除权除息日)，bonus 为每行的 (送股, 转增)。"""
    pairs = bonus if bonus is not None else [(0.0, 0.0)] * len(rows)
    return pd.DataFrame(
        {
            "公告日期": [pd.Timestamp(r[0]).date() for r in rows],
            "送股": [p[0] for p in pairs],
            "转增": [p[1] for p in pairs],
            "派息": [r[1] for r in rows],
            "进度": [r[2] for r in rows],
            "除权除息日": [None if r[3] is None else pd.Timestamp(r[3]).date() for r in rows],
            "股权登记日": [None] * len(rows),
            "红股上市日": [pd.NaT] * len(rows),
        }
    )


def test_normalize_dividends_converts_per_10_shares_and_fiscal_year() -> None:
    raw = _raw_dividends(
        [
            ("2025-07-10", 4.10, "实施", "2025-07-16"),  # 7 月除权 → 上一年年报分红
            ("2024-07-11", 3.21, "实施", "2024-07-18"),
        ]
    )
    out = normalize_dividends("600000", raw)
    validate(Table.DIVIDENDS, out)
    assert out["cash_per_share"].tolist() == pytest.approx([0.321, 0.41])
    assert out["fiscal_year"].tolist() == [2023, 2024]
    assert out["announce_date"].tolist() == [pd.Timestamp("2024-07-11"), pd.Timestamp("2025-07-10")]
    assert out["ex_date"].tolist() == [pd.Timestamp("2024-07-18"), pd.Timestamp("2025-07-16")]


def test_dividends_without_history_convert_to_empty_frame() -> None:
    # 数据源对没有分红记录的股票返回无列空表
    sample = normalize_dividends(
        "600000", _raw_dividends([("2025-07-10", 4.10, "实施", "2025-07-16")])
    )
    out = normalize_dividends("603293", pd.DataFrame())
    assert list(out.columns) == list(sample.columns)
    assert len(out) == 0
    validate(Table.DIVIDENDS, out)


def test_dividends_missing_ex_date_column_convert_to_empty_frame() -> None:
    raw = pd.DataFrame({"公告日期": ["2025-07-10"], "派息": [4.1], "进度": ["实施"]})
    out = normalize_dividends("603293", raw)
    assert len(out) == 0
    validate(Table.DIVIDENDS, out)


def test_fiscal_year_month_boundary() -> None:
    assert fiscal_year_of(date(2024, 8, 31)) == 2023
    assert fiscal_year_of(date(2024, 9, 1)) == 2024
    assert fiscal_year_of(date(2024, 1, 1)) == 2023
    assert fiscal_year_of(date(2024, 12, 31)) == 2024
    raw = _raw_dividends(
        [
            ("2024-08-20", 1.0, "实施", "2024-08-30"),
            ("2024-09-10", 2.0, "实施", "2024-09-20"),
        ]
    )
    out = normalize_dividends("600519", raw)
    assert out["fiscal_year"].tolist() == [2023, 2024]


def test_normalize_dividends_drops_unimplemented_and_zero_rows() -> None:
    raw = _raw_dividends(
        [
            ("2024-03-01", 0.0, "不分配", None),
            ("2024-03-01", 5.0, "预案", None),
            ("2024-04-01", 5.0, "预案", "2024-06-01"),  # 非“实施”
            ("2024-05-01", 0.0, "实施", "2024-06-02"),  # 派息为 0（纯送转）
            ("2024-05-02", 6.0, "实施", None),  # 无除权日
            ("2023-07-01", 2.0, "实施", "2023-07-10"),
        ]
    )
    out = normalize_dividends("000001", raw)
    assert out["ex_date"].tolist() == [pd.Timestamp("2023-07-10")]
    assert out["cash_per_share"].tolist() == [0.2]
    assert len(normalize_dividends("000001", raw.iloc[0:0])) == 0


def test_normalize_dividends_merges_same_ex_date() -> None:
    raw = _raw_dividends(
        [
            ("2024-06-01", 3.0, "实施", "2024-06-20"),
            ("2024-06-05", 1.0, "实施", "2024-06-20"),
        ]
    )
    out = normalize_dividends("600036", raw)
    assert len(out) == 1
    assert out["cash_per_share"].iloc[0] == pytest.approx(0.4)
    assert out["announce_date"].iloc[0] == pd.Timestamp("2024-06-01")
    assert out["bonus_per_share"].iloc[0] == 0.0


def test_normalize_dividends_keeps_bonus_shares() -> None:
    raw = _raw_dividends(
        [
            ("2020-07-10", 5.0, "实施", "2020-07-17"),  # 10 派 5 + 10 转 10
            ("2021-06-01", 0.0, "实施", "2021-06-10"),  # 纯送转：10 送 2 转 3
            ("2022-06-01", 0.0, "实施", "2022-06-10"),  # 既无派息也无送转：丢弃
            ("2023-06-01", 2.0, "实施", "2023-06-10"),
        ],
        bonus=[(0.0, 10.0), (2.0, 3.0), (0.0, 0.0), (float("nan"), float("nan"))],
    )
    out = normalize_dividends("600000", raw)
    validate(Table.DIVIDENDS, out)
    assert out["ex_date"].tolist() == [
        pd.Timestamp("2020-07-17"),
        pd.Timestamp("2021-06-10"),
        pd.Timestamp("2023-06-10"),
    ]
    assert out["cash_per_share"].tolist() == pytest.approx([0.5, 0.0, 0.2])
    assert out["bonus_per_share"].tolist() == pytest.approx([1.0, 0.5, 0.0])


def test_normalize_dividends_sums_bonus_on_same_ex_date() -> None:
    raw = _raw_dividends(
        [
            ("2024-06-01", 3.0, "实施", "2024-06-20"),
            ("2024-06-05", 0.0, "实施", "2024-06-20"),
        ],
        bonus=[(1.0, 0.0), (0.0, 4.0)],
    )
    out = normalize_dividends("600036", raw)
    assert len(out) == 1
    assert out["cash_per_share"].iloc[0] == pytest.approx(0.3)
    assert out["bonus_per_share"].iloc[0] == pytest.approx(0.5)


# ---------------------------------------------------------------- financials


def test_parse_helpers_handle_ths_traps() -> None:
    assert parse_percent("3.96%") == pytest.approx(0.0396)
    assert parse_percent("-12.5%") == pytest.approx(-0.125)
    for bad in (False, True, None, float("nan"), "", "--", "abc%", "5.2", "inf%"):
        assert math.isnan(parse_percent(bad)), bad
    assert parse_amount("309.51亿") == pytest.approx(3.0951e10)
    assert parse_amount("-1,234.5万") == pytest.approx(-12_345_000.0)
    assert parse_amount("5000") == 5000.0
    assert parse_amount("1.5万亿") == pytest.approx(1.5e12)
    for bad in (False, None, "False", "亿", "--"):
        assert math.isnan(parse_amount(bad)), bad
    assert parse_plain("0.5200") == 0.52
    assert math.isnan(parse_plain(False))


def _raw_ths(rows: list[dict[str, object]]) -> pd.DataFrame:
    cols = ["报告期", "净利润", "净利润同比增长率", "基本每股收益", "净资产收益率", "资产负债率"]
    return pd.DataFrame([{c: r.get(c, False) for c in cols} for r in rows])


def test_normalize_financials_units_and_missing_values() -> None:
    raw = _raw_ths(
        [
            {
                "报告期": "2026-09-30",
                "净利润": "405.72亿",
                "净利润同比增长率": "4.52%",
                "基本每股收益": "1.1700",
                "净资产收益率": "5.20%",
                "资产负债率": "91.91%",
            },
            {"报告期": "2025-12-31", "净利润": "12.3万"},  # 其余全是 False
        ]
    )
    out = normalize_financials("600000", raw)
    validate(Table.FINANCIALS, out)
    q3 = out[out["report_period"] == pd.Timestamp("2026-09-30")].iloc[0]
    assert q3["net_profit"] == pytest.approx(4.0572e10)
    assert q3["roe"] == pytest.approx(0.052)
    assert q3["net_profit_yoy"] == pytest.approx(0.0452)
    assert q3["debt_ratio"] == pytest.approx(0.9191)
    assert q3["eps"] == pytest.approx(1.17)
    annual = out[out["report_period"] == pd.Timestamp("2025-12-31")].iloc[0]
    assert annual["net_profit"] == pytest.approx(123_000.0)
    for col in ("roe", "net_profit_yoy", "debt_ratio", "eps"):
        assert math.isnan(annual[col]), col


def test_statutory_deadline_for_each_report_type() -> None:
    assert statutory_deadline(date(2023, 12, 31)) == date(2024, 4, 30)
    assert statutory_deadline(date(2024, 3, 31)) == date(2024, 4, 30)
    assert statutory_deadline(date(2024, 6, 30)) == date(2024, 8, 31)
    assert statutory_deadline(date(2024, 9, 30)) == date(2024, 10, 31)
    assert statutory_deadline(date(2024, 5, 31)) is None
    raw = _raw_ths(
        [{"报告期": p} for p in ("2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30")]
    )
    out = normalize_financials("600000", raw)
    assert [d.date() for d in out["announce_date"]] == [
        date(2024, 4, 30),
        date(2024, 4, 30),
        date(2024, 8, 31),
        date(2024, 10, 31),
    ]


def test_normalize_financials_prefers_actual_disclosure_date() -> None:
    raw = _raw_ths([{"报告期": "2023-12-31"}, {"报告期": "2024-03-31"}])
    out = normalize_financials("600000", raw, {date(2023, 12, 31): date(2024, 3, 15)})
    by_period = {
        pd.Timestamp(p).date(): pd.Timestamp(a).date()
        for p, a in zip(out["report_period"], out["announce_date"], strict=True)
    }
    assert by_period[date(2023, 12, 31)] == date(2024, 3, 15)  # 实际披露日
    assert by_period[date(2024, 3, 31)] == date(2024, 4, 30)  # 没有披露记录：法定截止日
    # 早于报告期的披露日不可信，退回法定截止日
    bad = normalize_financials("600000", raw, {date(2023, 12, 31): date(2023, 1, 1)})
    assert bad["announce_date"].iloc[0] == pd.Timestamp("2024-04-30")


def test_period_label_and_parse_disclosure() -> None:
    assert period_label(date(2012, 9, 30)) == "2012三季"
    assert period_label(date(2012, 6, 30)) == "2012半年报"
    assert period_label(date(2012, 3, 31)) == "2012一季"
    assert period_label(date(2012, 12, 31)) == "2012年报"
    assert period_label(date(2012, 5, 31)) is None
    raw = pd.DataFrame(
        {
            "股票代码": ["000001", "600000", "600001"],
            "实际披露": [date(2024, 3, 15), pd.NaT, date(2024, 4, 1)],
        }
    )
    assert parse_disclosure(raw) == {"000001": date(2024, 3, 15), "600001": date(2024, 4, 1)}


# ---------------------------------------------------------------- securities


def test_board_of_code() -> None:
    assert board_of_code("600000") == "sh_main"
    assert board_of_code("605001") == "sh_main"
    assert board_of_code("002415") == "sz_main"
    assert board_of_code("300750") == "chinext"
    assert board_of_code("688001") is None
    assert board_of_code("200002") is None
    assert board_of_code("900901") is None


def _listing_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sh = pd.DataFrame(
        {"证券代码": ["600000"], "证券简称": ["浦发银行"], "上市日期": ["1999-11-10"]}
    )
    sz = pd.DataFrame(
        {
            "板块": ["主板", "创业板", "科创板"],
            "A股代码": ["000001", "300750", "399999"],
            "A股简称": ["平安银行", "宁德时代", "某某"],
            "A股上市日期": ["1991-04-03", "2018-06-11", "2020-01-01"],
        }
    )
    sh_d = pd.DataFrame(
        {
            "公司代码": ["600001", "900901", "688099"],
            "公司简称": ["邯郸钢铁", "B股", "科创"],
            "上市日期": ["1998-01-22", "1992-01-01", "2020-01-01"],
            "暂停上市日期": ["2009-12-29", "2020-01-01", "2021-01-01"],
        }
    )
    sz_d = pd.DataFrame(
        {
            "证券代码": ["000004", "200002", "300104"],
            "证券简称": ["国华退", "B股", "乐视退"],
            "上市日期": ["1990-12-01", "1992-01-01", "2010-08-12"],
            "终止上市日期": ["2026-07-14", "2020-01-01", "2020-07-20"],
        }
    )
    return sh, sz, sh_d, sz_d


def test_normalize_securities_boards_and_delisted() -> None:
    sh, sz, sh_d, sz_d = _listing_frames()
    out = normalize_securities(sh, sz, sh_d, sz_d, ["sh_main", "sz_main"])
    validate(Table.SECURITIES, out)
    rows = out.set_index("symbol")
    assert sorted(rows.index) == ["000001", "000004", "600000", "600001"]
    assert rows.loc["600000", "board"] == "sh_main"
    assert rows.loc["000001", "board"] == "sz_main"
    assert rows.loc["000004", "board"] == "sz_main"
    assert bool(rows["delist_date"].isna().loc["600000"])
    assert rows.loc["600001", "delist_date"] == pd.Timestamp("2009-12-29")
    assert rows.loc["000004", "delist_date"] == pd.Timestamp("2026-07-14")
    assert rows.loc["600000", "list_date"] == pd.Timestamp("1999-11-10")
    with_chinext = normalize_securities(sh, sz, sh_d, sz_d, ["sh_main", "sz_main", "chinext"])
    boards = dict(zip(with_chinext["symbol"], with_chinext["board"], strict=True))
    assert boards["300750"] == "chinext"
    assert boards["300104"] == "chinext"
    assert "399999" not in boards


def test_normalize_securities_prefers_listed_over_delisted_duplicate() -> None:
    sh, sz, sh_d, sz_d = _listing_frames()
    sh_d.loc[0, "公司代码"] = "600000"
    out = normalize_securities(sh, sz, sh_d, sz_d, ["sh_main", "sz_main"])
    row = out[out["symbol"] == "600000"].iloc[0]
    assert pd.isna(row["delist_date"])


# ---------------------------------------------------------------- industries / st / benchmarks


def _raw_industry() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "分类标准": [
                "巨潮行业分类标准",
                "证监会行业分类标准（2012）",
                "证监会行业分类标准（2012）",
            ],
            "行业门类": ["金融", "制造业", "金融业"],
            "行业大类": ["银行", "旧分类", "货币金融服务"],
            "变更日期": ["2019-05-08", "1991-04-03", "2024-01-01"],
        }
    )


def test_classify_industry_uses_latest_csrc_row_and_financial_rule() -> None:
    assert classify_industry(_raw_industry()) == ("货币金融服务", True)
    other = pd.DataFrame(
        {
            "分类标准": ["证监会行业分类标准（2012）"],
            "行业门类": ["制造业"],
            "行业大类": ["酒、饮料和精制茶制造业"],
            "变更日期": ["2001-08-27"],
        }
    )
    assert classify_industry(other) == ("酒、饮料和精制茶制造业", False)
    assert classify_industry(_raw_industry().iloc[[0]]) is None
    assert classify_industry(pd.DataFrame()) is None


def test_build_industries_skips_unclassified() -> None:
    out = build_industries({"600000": ("货币金融服务", True), "600519": ("制造", False)})
    validate(Table.INDUSTRIES, out)
    assert out["symbol"].tolist() == ["600000", "600519"]
    assert out["is_financial"].tolist() == [True, False]
    assert len(build_industries({})) == 0


def test_normalize_st_history_only_current_st_names() -> None:
    names = pd.DataFrame(
        {
            "code": ["000010", "000056", "600000", "000078", "600001"],
            "name": ["*ST美丽", "*ST皇庭", "浦发银行", "ST海王", "邯郸钢铁"],
        }
    )
    out = normalize_st_history(names, date(2026, 11, 12))
    validate(Table.ST_HISTORY, out)
    assert out["symbol"].tolist() == ["000010", "000056", "000078"]
    assert (out["start"] == pd.Timestamp("2026-11-12")).all()
    assert out["end"].isna().all()


def test_normalize_csindex_maps_columns() -> None:
    raw = pd.DataFrame(
        {
            "日期": ["2009-01-05", "2009-01-06", "2009-01-07"],
            "指数代码": ["H00300"] * 3,
            "收盘": [1950.97, 1990.0, float("nan")],
        }
    )
    out = normalize_csindex("H00300", raw)
    validate(Table.BENCHMARKS, out)
    assert out["code"].tolist() == ["H00300", "H00300"]
    assert out["close"].tolist() == [1950.97, 1990.0]


def test_etf_total_return_reinvests_dividend_on_ex_date() -> None:
    prices = pd.DataFrame(
        {
            "date": ["2025-01-17", "2025-01-20", "2025-01-21", "2025-01-22"],
            "close": [3.200, 3.185, 3.027, 3.040],
        }
    )
    paid = pd.DataFrame({"日期": ["2024-01-23", "2025-01-21"], "累计分红": [1.291, 1.433]})
    out = build_etf_total_return("510880", prices, paid)
    validate(Table.BENCHMARKS, out)
    level = out["close"].tolist()
    assert level[0] == 3.200
    assert level[1] == pytest.approx(3.200 * 3.185 / 3.200)
    # 除息日：再投资 0.142 的分红，净值只反映 (3.027 + 0.142) / 3.185
    assert level[2] == pytest.approx(level[1] * (3.027 + 0.142) / 3.185)
    assert level[3] == pytest.approx(level[2] * 3.040 / 3.027)


def test_etf_total_return_dividend_on_non_trading_day_moves_to_next_bar() -> None:
    prices = pd.DataFrame({"date": ["2025-01-20", "2025-01-22"], "close": [3.0, 2.9]})
    paid = pd.DataFrame({"日期": ["2025-01-21"], "累计分红": [0.1]})
    out = build_etf_total_return("510880", prices, paid)
    assert out["close"].iloc[1] == pytest.approx(3.0 * (2.9 + 0.1) / 3.0)


# ---------------------------------------------------------------- 重试


def test_call_with_retry_retries_then_succeeds() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("断线")
        return "ok"

    assert call_with_retry(flaky, retries=3, pause_seconds=0.5, sleep=sleeps.append) == "ok"
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0, 0.5]


def test_call_with_retry_gives_up_after_retries() -> None:
    attempts: list[int] = []

    def broken() -> str:
        attempts.append(1)
        raise ConnectionError("断线")

    with pytest.raises(ConnectionError):
        call_with_retry(broken, retries=3, pause_seconds=0.0, sleep=lambda _: None)
    assert len(attempts) == 3


# ---------------------------------------------------------------- 复审修订


def test_normalize_bars_rejects_empty_factor_table() -> None:
    raw = _raw_daily([(date(2024, 1, 2), 5.0, 1e6, 5e6)])
    with pytest.raises(ValueError, match="复权因子"):
        normalize_bars("000001", raw, _events([]))


def test_normalize_securities_marks_delisted_rows_without_date() -> None:
    sh, sz, sh_d, sz_d = _listing_frames()
    sh_d.loc[0, "暂停上市日期"] = None
    out = normalize_securities(sh, sz, sh_d, sz_d, ["sh_main", "sz_main"])
    row = out.set_index("symbol").loc["600001"]
    assert bool(row["delist_date"] == UNKNOWN_DELIST_DATE)  # 绝不能是 NaT（会被当成在市）


def test_classify_industry_rejects_unexpected_shape() -> None:
    odd = pd.DataFrame({"某列": [1]})
    with pytest.raises(ValueError, match="列"):
        classify_industry(odd)


def test_call_with_retry_does_not_sleep_after_final_failure() -> None:
    sleeps: list[float] = []

    def broken() -> str:
        raise ConnectionError("断线")

    with pytest.raises(ConnectionError):
        call_with_retry(broken, retries=3, pause_seconds=0.5, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0]


def test_call_with_retry_does_not_retry_fatal_errors() -> None:
    attempts: list[int] = []

    def odd_shape() -> str:
        attempts.append(1)
        raise KeyError("变更日期")

    with pytest.raises(KeyError):
        call_with_retry(odd_shape, 3, 0.0, sleep=lambda _: None, fatal=(KeyError,))
    assert len(attempts) == 1


# ---------------------------------------------------------------- 墙钟时限


def test_hung_call_times_out_on_every_attempt() -> None:
    release = threading.Event()
    calls: list[int] = []
    sleeps: list[float] = []

    def hang() -> int:
        calls.append(1)
        release.wait(5)
        return 1

    try:
        with pytest.raises(RequestTimeoutError, match="秒"):
            call_with_retry(hang, 2, 0.0, sleep=sleeps.append, timeout_seconds=0.05)
        assert len(calls) == 2  # 超时算一次失败的尝试，会重试
    finally:
        release.set()


def test_timed_out_attempt_is_retried_and_can_succeed() -> None:
    release = threading.Event()
    calls: list[int] = []

    def first_hangs() -> str:
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
        return "ok"

    try:
        assert call_with_retry(first_hangs, 3, 0.0, sleep=lambda _: None, timeout_seconds=0.05)
        assert len(calls) == 2
    finally:
        release.set()


def test_deadline_passes_through_results_and_errors() -> None:
    assert call_with_deadline(lambda: 42, 1.0) == 42

    def boom() -> int:
        raise ConnectionError("断线")

    with pytest.raises(ConnectionError, match="断线"):
        call_with_deadline(boom, 1.0)


# ---------------------------------------------------------------- 批量行情


def _spot(rows: list[tuple[object, ...]]) -> pd.DataFrame:
    """新浪 stock_zh_a_spot 的原始表（列取自 2026-11-16 的实测）。"""
    cols = ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "买入", "卖出", "昨收", "今开", "最高"]
    cols += ["最低", "成交量", "成交额", "时间戳"]
    return pd.DataFrame(rows, columns=cols)


def test_normalize_daily_snapshot_maps_columns_and_strips_prefix() -> None:
    raw = _spot(
        [
            (
                "sh600000",
                "浦发银行",
                10.5,
                0.1,
                0.96,
                10.49,
                10.5,
                10.4,
                10.41,
                10.6,
                10.3,
                12345600.0,
                129876543.21,
                "15:00:00",
            ),
            (
                "sz000001",
                "平安银行",
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                11.2,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                "15:00:00",
            ),
            (
                "bj920000",
                "北交所",
                "8.8",
                0.0,
                0.0,
                0.0,
                0.0,
                "8.8",
                "8.7",
                "8.9",
                "8.6",
                "1000",
                "8800",
                "15:00:00",
            ),
        ]
    )
    out = normalize_daily_snapshot(raw)
    assert list(out.columns) == list(SNAPSHOT_SCHEMA)
    assert [str(t) for t in out.dtypes[1:]] == ["float64"] * 7
    assert out["symbol"].tolist() == ["600000", "000001", "920000"]
    first = out.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (10.41, 10.6, 10.3, 10.5)
    assert (first["prev_close"], first["volume"], first["amount"]) == (
        10.4,
        12345600.0,
        129876543.21,
    )
    assert out.iloc[1]["volume"] == 0.0 and out.iloc[1]["prev_close"] == 11.2  # 停牌
    assert out.iloc[2]["close"] == 8.8


def test_normalize_daily_snapshot_drops_duplicates_and_bad_codes() -> None:
    row = ("sh600000", "甲", 10.0, 0, 0, 0, 0, 9.9, 9.95, 10.1, 9.9, 100.0, 1000.0, "15:00:00")
    other = ("sh600001", "乙", 5.0, 0, 0, 0, 0, 4.9, 4.95, 5.1, 4.9, 100.0, 500.0, "15:00:00")
    bad = ("xx", "丙", 5.0, 0, 0, 0, 0, 4.9, 4.95, 5.1, 4.9, 100.0, 500.0, "15:00:00")
    out = normalize_daily_snapshot(_spot([row, other, row, bad]))
    assert out["symbol"].tolist() == ["600001"]  # 重复的代码无法判断哪行可信，全部丢弃


def test_normalize_daily_snapshot_rejects_missing_columns() -> None:
    raw = _spot([]).drop(columns=["昨收"])
    with pytest.raises(ValueError, match="昨收"):
        normalize_daily_snapshot(raw)


def test_daily_snapshot_is_attempted_once_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def throttled() -> pd.DataFrame:
        calls.append(1)
        raise ConnectionError("模拟被限流")

    monkeypatch.setattr("akshare.stock_zh_a_spot", throttled)
    cfg = default_app_config()
    data = cfg.data.model_copy(update={"request_pause_seconds": 0.0, "retries": 3})
    source = AkshareSource(data, cfg.universe.boards, cfg.backtest.data_start)
    with pytest.raises(ConnectionError):
        source.daily_snapshot()
    assert len(calls) == 1
