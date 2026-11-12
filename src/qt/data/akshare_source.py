# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportMissingTypeStubs=false
"""AKShare 数据源。接口选择、列名与单位以 docs/data-gaps.md 为准。

每个“AKShare 原始表 → 标准表”的转换都是不联网的纯函数（normalize_* 等），
AkshareSource 只是带重试和间隔的薄封装。本文件是唯一做列名映射和类型清洗的地方。
"""

import math
import re
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, cast

import akshare as ak
import pandas as pd

from qt.config import AppConfig, DataConfig
from qt.data.schema import SCHEMAS, Table, empty_frame, validate

# 证监会行业分类标准（2012）在巨潮表里的分类标准名称
CSRC_STANDARD = "证监会行业分类标准（2012）"
# 巨潮披露表的报告期标签
_PERIOD_LABELS = {(12, 31): "年报", (3, 31): "一季", (6, 30): "半年报", (9, 30): "三季"}
_NOT_A_NUMBER = float("nan")


# ---------------------------------------------------------------- 通用清洗


def _as_type(series: pd.Series, dtype: str) -> pd.Series:
    """把列转成指定类型（pandas-stubs 不接受字符串 dtype，这里集中处理）。"""
    return cast(pd.Series, series.astype(cast(Any, dtype)))


def _dates(values: pd.Series) -> pd.Series:
    """各种日期表示（date、字符串、None）转 datetime64[ns]，无法解析的变 NaT。"""
    return _as_type(pd.to_datetime(values, errors="coerce"), "datetime64[ns]")


def _symbols(values: pd.Series) -> pd.Series:
    """代码补齐为 6 位字符串。"""
    return _as_type(_as_type(values, "str").str.strip().str.zfill(6), "str")


def _numbers(values: pd.Series) -> pd.Series:
    """转 float64，无法解析的变 NaN。"""
    return _as_type(pd.to_numeric(values, errors="coerce"), "float64")


def _clean_text(value: object) -> str | None:
    """取出可解析的文本；缺失值（None、NaN、布尔 False/True、空串、占位符）返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "nan", "NaN", "None", "False", "True"):
        return None
    return text


def _finite(value: float) -> float:
    return value if math.isfinite(value) else _NOT_A_NUMBER


def parse_percent(value: object) -> float:
    """百分比字符串（如 "3.96%"）转小数 0.0396；缺失或无法解析返回 NaN，绝不返回 0。

    没有百分号的值无法确定是否已是小数，也按无法解析处理。
    """
    text = _clean_text(value)
    if text is None or not text.endswith("%"):
        return _NOT_A_NUMBER
    try:
        return _finite(float(text[:-1]) / 100)
    except ValueError:
        return _NOT_A_NUMBER


def parse_amount(value: object) -> float:
    """金额字符串（如 "309.51亿"、"-12万"、"5000"）转元；缺失或无法解析返回 NaN。"""
    text = _clean_text(value)
    if text is None:
        return _NOT_A_NUMBER
    scale = 1.0
    if text.endswith("万亿"):
        scale, text = 1e12, text[:-2]
    elif text.endswith("亿"):
        scale, text = 1e8, text[:-1]
    elif text.endswith("万"):
        scale, text = 1e4, text[:-1]
    try:
        return _finite(float(text) * scale)
    except ValueError:
        return _NOT_A_NUMBER


def parse_plain(value: object) -> float:
    """普通数值（如每股收益 "0.5200"）转 float；缺失或无法解析返回 NaN。"""
    text = _clean_text(value)
    if text is None:
        return _NOT_A_NUMBER
    try:
        return _finite(float(text))
    except ValueError:
        return _NOT_A_NUMBER


def call_with_retry[T](
    fn: Callable[[], T],
    retries: int,
    pause_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """调用 fn，失败则间隔后重试，最多尝试 retries 次；成功后也停顿 pause_seconds。"""
    last: Exception | None = None
    for attempt in range(1, max(retries, 1) + 1):
        try:
            result = fn()
        except Exception as exc:  # 网络接口可能抛任何异常，统一重试
            last = exc
            sleep(pause_seconds * attempt)
        else:
            sleep(pause_seconds)
            return result
    assert last is not None
    raise last


# ---------------------------------------------------------------- securities


def board_of_code(symbol: str) -> str | None:
    """按代码前缀判断板块；B 股、科创板、北交所等不支持的返回 None。"""
    if symbol[:3] in ("600", "601", "603", "605"):
        return "sh_main"
    if symbol[:3] in ("000", "001", "002", "003"):
        return "sz_main"
    if symbol[:3] in ("300", "301"):
        return "chinext"
    return None


def _securities_part(
    codes: pd.Series, names: pd.Series, listed: pd.Series, delisted: pd.Series, boards: pd.Series
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": _symbols(codes).reset_index(drop=True),
            "name": _as_type(names, "str").str.strip().reset_index(drop=True),
            "board": _as_type(boards, "str").reset_index(drop=True),
            "list_date": _dates(listed).reset_index(drop=True),
            "delist_date": _dates(delisted).reset_index(drop=True),
        }
    )


def normalize_securities(
    sh_listed: pd.DataFrame,
    sz_listed: pd.DataFrame,
    sh_delisted: pd.DataFrame,
    sz_delisted: pd.DataFrame,
    boards: Sequence[str],
) -> pd.DataFrame:
    """四张交易所列表合成 securities。

    在市：沪市全部为 sh_main；深市看 `板块`（主板 → sz_main，创业板 → chinext）。
    退市：表里没有板块，按代码前缀判断，并滤掉 B 股、科创板等；沪市退市日期是
    `暂停上市日期`，深市是 `终止上市日期`。同一代码在市与退市同时出现时以在市为准。
    只保留 boards 中的板块。
    """
    sh_board = pd.Series(["sh_main"] * len(sh_listed), dtype="str")
    sz_map = {"主板": "sz_main", "创业板": "chinext"}
    sz_board = _as_type(sz_listed["板块"].map(sz_map), "str")
    none = pd.Series([pd.NaT] * len(sh_listed))
    sz_none = pd.Series([pd.NaT] * len(sz_listed))
    parts = [
        _securities_part(
            sh_listed["证券代码"], sh_listed["证券简称"], sh_listed["上市日期"], none, sh_board
        ),
        _securities_part(
            sz_listed["A股代码"], sz_listed["A股简称"], sz_listed["A股上市日期"], sz_none, sz_board
        ),
    ]
    for frame, code, name, listed, delisted in (
        (sh_delisted, "公司代码", "公司简称", "上市日期", "暂停上市日期"),
        (sz_delisted, "证券代码", "证券简称", "上市日期", "终止上市日期"),
    ):
        symbols = _symbols(frame[code])
        derived = _as_type(symbols.map(board_of_code), "str")
        parts.append(
            _securities_part(symbols, frame[name], frame[listed], frame[delisted], derived)
        )
    out = pd.concat(parts, ignore_index=True)
    out = out[out["board"].isin(list(boards))]
    out = out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    validate(Table.SECURITIES, out)
    return out


# ---------------------------------------------------------------- bars


def normalize_bars(symbol: str, raw: pd.DataFrame, factor_events: pd.DataFrame) -> pd.DataFrame:
    """新浪未复权日线 + hfq-factor 事件表 → bars。

    raw 列：date, open, high, low, close, volume（股）, amount（元）等；
    factor_events 列：date, hfq_factor（字符串），每个除权日一行，含 1900-01-01 的基准行。
    adj_factor = 事件表按日期前向填充到交易日，使 后复权收盘价 = close * adj_factor；
    比两列价格相除精确（价格只保留两位小数）。第一个事件之前的日子取 1.0。
    """
    if len(raw) == 0:
        return empty_frame(Table.BARS)
    days = _dates(raw["date"])
    events = pd.DataFrame(
        {"date": _dates(factor_events["date"]), "factor": _numbers(factor_events["hfq_factor"])}
    ).dropna()
    events = events.sort_values("date").drop_duplicates("date", keep="last")
    positions = events["date"].searchsorted(days, side="right")
    factors = events["factor"].to_numpy()
    adj = [float(factors[p - 1]) if p > 0 else 1.0 for p in positions]
    out = pd.DataFrame(
        {
            "symbol": pd.Series([symbol] * len(raw), dtype="str"),
            "day": days.reset_index(drop=True),
            "open": _numbers(raw["open"]).reset_index(drop=True),
            "high": _numbers(raw["high"]).reset_index(drop=True),
            "low": _numbers(raw["low"]).reset_index(drop=True),
            "close": _numbers(raw["close"]).reset_index(drop=True),
            "volume": _numbers(raw["volume"]).reset_index(drop=True),  # 新浪已是股
            "amount": _numbers(raw["amount"]).reset_index(drop=True),  # 元
            "adj_factor": pd.Series(adj, dtype="float64"),
        }
    )
    out = out.dropna(subset=["day", "close"]).drop_duplicates("day", keep="last")
    out = out.reset_index(drop=True)
    validate(Table.BARS, out)
    return out


# ---------------------------------------------------------------- dividends


def fiscal_year_of(ex_date: date) -> int:
    """按除权除息日推断会计年度：1 到 8 月 → 上一年（年报分红），9 到 12 月 → 当年（中期、三季度）。

    规则与验证见 docs/data-gaps.md 第 5 节，是近似（留出样本约 99% 与东财报告期一致）。
    """
    return ex_date.year - 1 if ex_date.month <= 8 else ex_date.year


def normalize_dividends(symbol: str, raw: pd.DataFrame) -> pd.DataFrame:
    """新浪分红表 → dividends。

    只保留 `进度=="实施"`、`除权除息日` 非空、`派息>0` 的行；`派息` 是元/10 股（税前），除以 10
    得每股元。announce_date 取 `公告日期`（实施公告日，晚于预案日），缺失时退回除权日（更保守）。
    同一除权日有多行（例如年报分红加特别分红）时合并：派息相加，公告日取最早。
    """
    ex = _dates(raw["除权除息日"])
    cash = _numbers(raw["派息"])
    keep = (_as_type(raw["进度"], "str").str.strip() == "实施") & ex.notna() & (cash > 0)
    announce = _dates(raw["公告日期"])
    frame = pd.DataFrame(
        {
            "announce_date": announce.where(announce.notna(), ex)[keep],
            "ex_date": ex[keep],
            "cash_per_share": (cash / 10)[keep],
        }
    )
    if len(frame) == 0:
        return empty_frame(Table.DIVIDENDS)
    grouped = frame.groupby("ex_date", as_index=False).agg(
        announce_date=("announce_date", "min"), cash_per_share=("cash_per_share", "sum")
    )
    grouped["fiscal_year"] = [fiscal_year_of(ts.date()) for ts in grouped["ex_date"]]
    grouped["symbol"] = symbol
    out = grouped[list(SCHEMAS[Table.DIVIDENDS])].copy()
    out["symbol"] = _as_type(out["symbol"], "str")
    out["fiscal_year"] = _as_type(out["fiscal_year"], "int64")
    out["announce_date"] = _as_type(out["announce_date"], "datetime64[ns]")
    out["ex_date"] = _as_type(out["ex_date"], "datetime64[ns]")
    out["cash_per_share"] = _as_type(out["cash_per_share"], "float64")
    validate(Table.DIVIDENDS, out)
    return out


# ---------------------------------------------------------------- financials


def statutory_deadline(period_end: date) -> date | None:
    """法定披露截止日：年报次年 4/30，一季报 4/30，半年报 8/31，三季报 10/31。

    非季末的报告期返回 None。
    """
    key = (period_end.month, period_end.day)
    if key == (12, 31):
        return date(period_end.year + 1, 4, 30)
    if key == (3, 31):
        return date(period_end.year, 4, 30)
    if key == (6, 30):
        return date(period_end.year, 8, 31)
    if key == (9, 30):
        return date(period_end.year, 10, 31)
    return None


def period_label(period_end: date) -> str | None:
    """巨潮披露表的报告期参数，如 date(2012,9,30) → "2012三季"；非季末返回 None。"""
    suffix = _PERIOD_LABELS.get((period_end.month, period_end.day))
    return None if suffix is None else f"{period_end.year}{suffix}"


def parse_disclosure(raw: pd.DataFrame) -> dict[str, date]:
    """巨潮披露表 → {股票代码: 实际披露日}；实际披露为空的行不收录。"""
    codes = _symbols(raw["股票代码"])
    actual = _dates(raw["实际披露"])
    return {str(c): ts.date() for c, ts in zip(codes, actual, strict=True) if pd.notna(ts)}


def normalize_financials(
    symbol: str, raw: pd.DataFrame, disclosure: Mapping[date, date] | None = None
) -> pd.DataFrame:
    """同花顺“按报告期”指标表 → financials。

    disclosure 是该股票 {报告期: 实际披露日}（来自巨潮）；没有时按法定截止日。
    百分比列 `净资产收益率`、`净利润同比增长率`、`资产负债率` 转小数；`净利润` 的万/亿后缀换算成元；
    `基本每股收益` 为元。缺失值（同花顺用布尔 False）与无法解析的单元格一律为 NaN，绝不为 0。
    注意：同花顺指标接口返回最新口径（含事后重述）的数值，而 announce_date 是首次披露日，
    重述后的数字在回测里被当作首次披露时就已知，这是已知局限。
    """
    disclosure = disclosure or {}
    periods = _dates(raw["报告期"])
    rows: list[dict[str, object]] = []
    for i, ts in enumerate(periods):
        if pd.isna(ts):
            continue
        period = ts.date()
        deadline = statutory_deadline(period)
        if deadline is None:
            continue
        actual = disclosure.get(period)
        announce = actual if actual is not None and actual >= period else deadline
        rows.append(
            {
                "symbol": symbol,
                "report_period": pd.Timestamp(period),
                "announce_date": pd.Timestamp(announce),
                "roe": parse_percent(raw["净资产收益率"].iloc[i]),
                "net_profit": parse_amount(raw["净利润"].iloc[i]),
                "net_profit_yoy": parse_percent(raw["净利润同比增长率"].iloc[i]),
                "debt_ratio": parse_percent(raw["资产负债率"].iloc[i]),
                "eps": parse_plain(raw["基本每股收益"].iloc[i]),
            }
        )
    if not rows:
        return empty_frame(Table.FINANCIALS)
    out = pd.DataFrame(rows)
    for col, dtype in SCHEMAS[Table.FINANCIALS].items():
        out[col] = _as_type(out[col], dtype)
    out = out.drop_duplicates("report_period", keep="last").reset_index(drop=True)
    validate(Table.FINANCIALS, out[list(SCHEMAS[Table.FINANCIALS])])
    return out[list(SCHEMAS[Table.FINANCIALS])]


# ---------------------------------------------------------------- industries / st / benchmarks


def classify_industry(raw: pd.DataFrame) -> tuple[str, bool] | None:
    """巨潮行业变更表 → (行业大类, 是否金融)；没有证监会 2012 标准的行返回 None。

    取证监会行业分类标准（2012）里变更日期最新的一行。is_financial 的规则：`行业门类 == "金融业"`，
    即货币金融服务（银行）、资本市场服务（证券）、保险业、其他金融业都算金融股。
    """
    if len(raw) == 0 or "分类标准" not in raw.columns:
        return None
    rows = raw[raw["分类标准"] == CSRC_STANDARD].copy()
    if len(rows) == 0:
        return None
    rows["_when"] = _dates(rows["变更日期"])
    latest = rows.sort_values("_when").iloc[-1]
    industry = _clean_text(latest["行业大类"])
    if industry is None:
        return None
    return industry, _clean_text(latest["行业门类"]) == "金融业"


def build_industries(found: Mapping[str, tuple[str, bool]]) -> pd.DataFrame:
    """{代码: (行业, 是否金融)} → industries；没有分类的股票不出现。"""
    symbols = sorted(found)
    out = pd.DataFrame(
        {
            "symbol": pd.Series(symbols, dtype="str"),
            "industry": pd.Series([found[s][0] for s in symbols], dtype="str"),
            "is_financial": pd.Series([found[s][1] for s in symbols], dtype="bool"),
        }
    )
    validate(Table.INDUSTRIES, out)
    return out


_ST_NAME = re.compile(r"^(S?\*?ST|\*ST)", re.IGNORECASE)


def normalize_st_history(names: pd.DataFrame, today: date) -> pd.DataFrame:
    """当前名称带 ST 的股票 → st_history（start=今天，end 为空）。

    历史上的 ST 区间无法还原，一律视为未知，不写入。names 列：code, name。
    """
    is_st = _as_type(names["name"], "str").str.strip().map(lambda n: bool(_ST_NAME.match(str(n))))
    symbols = sorted(_symbols(names["code"])[is_st.to_numpy()].tolist())
    return pd.DataFrame(
        {
            "symbol": pd.Series(symbols, dtype="str"),
            "start": pd.Series([pd.Timestamp(today)] * len(symbols), dtype="datetime64[ns]"),
            "end": pd.Series([pd.NaT] * len(symbols), dtype="datetime64[ns]"),
        }
    )


def normalize_csindex(code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """中证指数表（列 日期、收盘）→ benchmarks；code 为 H00300，收盘是全收益指数点位。"""
    out = pd.DataFrame({"day": _dates(raw["日期"]), "close": _numbers(raw["收盘"])})
    out = out.dropna().drop_duplicates("day", keep="last").sort_values("day")
    out.insert(0, "code", pd.Series([code] * len(out), dtype="str", index=out.index))
    out = out.reset_index(drop=True)
    validate(Table.BENCHMARKS, out)
    return out


def build_etf_total_return(
    code: str, prices: pd.DataFrame, dividends: pd.DataFrame
) -> pd.DataFrame:
    """未复权 ETF 价格 + 累计分红表 → 总收益指数（除息日按收盘价再投资，不计红利税）。

    prices 列：date, close（新浪，未复权）；dividends 列：日期, 累计分红
    （每份累计，相邻差为单次分红）。
    指数起点等于首日收盘价；某日净值 = 前一日净值 * (收盘 + 当日每份分红) / 前一日收盘。
    不在价格表里的分红日并入其后第一个交易日。
    """
    px = pd.DataFrame({"day": _dates(prices["date"]), "close": _numbers(prices["close"])})
    px = px.dropna().drop_duplicates("day", keep="last").sort_values("day").reset_index(drop=True)
    if len(px) == 0:
        return empty_frame(Table.BENCHMARKS)
    paid = pd.DataFrame({"day": _dates(dividends["日期"]), "cum": _numbers(dividends["累计分红"])})
    paid = paid.dropna().sort_values("day").reset_index(drop=True)
    single = paid["cum"].diff().fillna(paid["cum"]).clip(lower=0.0)
    per_day = [0.0] * len(px)
    slots = px["day"].searchsorted(paid["day"], side="left")
    for slot, amount in zip(slots, single, strict=True):
        if slot < len(px):
            per_day[int(slot)] += float(amount)
    closes = px["close"].tolist()
    level = [closes[0]]
    for i in range(1, len(px)):
        level.append(level[-1] * (closes[i] + per_day[i]) / closes[i - 1])
    out = pd.DataFrame(
        {
            "code": pd.Series([code] * len(px), dtype="str"),
            "day": px["day"],
            "close": pd.Series(level, dtype="float64"),
        }
    )
    validate(Table.BENCHMARKS, out)
    return out


# ---------------------------------------------------------------- 网络封装


def _exchange_prefix(symbol: str) -> str:
    """新浪接口的代码前缀：上交所 sh，深交所 sz。"""
    return ("sh" if symbol.startswith("6") else "sz") + symbol


def _slice_days(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    mask = (df["day"] >= pd.Timestamp(start)) & (df["day"] <= pd.Timestamp(end))
    return df[mask].reset_index(drop=True)


class AkshareSource:
    """基于 AKShare（新浪、同花顺、巨潮、中证）的 DataSource。

    所有网络请求单线程、带重试，每次请求之后停顿 request_pause_seconds。
    """

    def __init__(self, data: DataConfig, boards: Sequence[str], data_start: date) -> None:
        self._retries = data.retries
        self._pause = data.request_pause_seconds
        self._boards = list(boards)
        self._data_start = data_start
        self._securities: pd.DataFrame | None = None
        self._disclosure: dict[date, dict[str, date]] = {}

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "AkshareSource":
        """按应用配置创建。"""
        return cls(cfg.data, cfg.universe.boards, cfg.backtest.data_start)

    def _call[T](self, fn: Callable[[], T]) -> T:
        return call_with_retry(fn, self._retries, self._pause)

    def trade_days(self) -> pd.DataFrame:
        raw = self._call(ak.tool_trade_date_hist_sina)
        out = pd.DataFrame({"day": _dates(raw["trade_date"])}).dropna()
        out = out.drop_duplicates().sort_values("day").reset_index(drop=True)
        validate(Table.TRADE_DAYS, out)
        return out

    def securities(self) -> pd.DataFrame:
        sh = self._call(lambda: ak.stock_info_sh_name_code(symbol="主板A股"))
        sz = self._call(lambda: ak.stock_info_sz_name_code(symbol="A股列表"))
        sh_d = self._call(lambda: ak.stock_info_sh_delist(symbol="全部"))
        sz_d = self._call(lambda: ak.stock_info_sz_delist(symbol="终止上市公司"))
        self._securities = normalize_securities(sh, sz, sh_d, sz_d, self._boards)
        return self._securities

    def bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        code = _exchange_prefix(symbol)
        with warnings.catch_warnings():
            # 已退市股票新浪没有股本数据，AKShare 会警告；我们不用 outstanding_share/turnover
            warnings.filterwarnings("ignore", message=".*股本数据.*", category=UserWarning)
            raw = self._call(
                lambda: ak.stock_zh_a_daily(
                    symbol=code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                )
            )
        if len(raw) == 0:
            return empty_frame(Table.BARS)
        events = self._call(lambda: ak.stock_zh_a_daily(symbol=code, adjust="hfq-factor"))
        return normalize_bars(symbol, raw, events)

    def dividends(self, symbol: str) -> pd.DataFrame:
        raw = self._call(lambda: ak.stock_history_dividend_detail(symbol=symbol, indicator="分红"))
        out = normalize_dividends(symbol, raw)
        return out[out["ex_date"] >= pd.Timestamp(self._data_start)].reset_index(drop=True)

    def _disclosure_dates(self, period: date) -> dict[str, date]:
        """巨潮某报告期的 {代码: 实际披露日}，按报告期缓存；尚未到期的报告期不请求。"""
        if period not in self._disclosure:
            label = period_label(period)
            if label is None or period > date.today():
                self._disclosure[period] = {}
            else:
                raw = self._call(lambda: ak.stock_report_disclosure(market="沪深京", period=label))
                self._disclosure[period] = parse_disclosure(raw)
        return self._disclosure[period]

    def financials(self, symbol: str) -> pd.DataFrame:
        raw = self._call(
            lambda: ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
        )
        periods = _dates(raw["报告期"]).dropna()
        actual: dict[date, date] = {}
        for ts in periods:
            period = ts.date()
            if period >= self._data_start:
                found = self._disclosure_dates(period).get(symbol)
                if found is not None:
                    actual[period] = found
        out = normalize_financials(symbol, raw, actual)
        keep = out["report_period"] >= pd.Timestamp(self._data_start)
        return out[keep].reset_index(drop=True)

    def industries(self) -> pd.DataFrame:
        """逐只查询在市股票的巨潮行业；已退市股票查不到行业，按无分类处理。"""
        secs = self._securities if self._securities is not None else self.securities()
        listed = secs[secs["delist_date"].isna()]["symbol"].tolist()
        found: dict[str, tuple[str, bool]] = {}
        for symbol in listed:
            today = date.today().strftime("%Y%m%d")
            try:
                raw = self._call(
                    lambda s=symbol, t=today: ak.stock_industry_change_cninfo(
                        symbol=s, start_date="19900101", end_date=t
                    )
                )
            except KeyError:  # 空表没有“变更日期”列：该股票没有行业信息
                continue
            classified = classify_industry(raw)
            if classified is not None:
                found[symbol] = classified
        return build_industries(found)

    def st_history(self) -> pd.DataFrame:
        names = self._call(ak.stock_info_a_code_name)
        return normalize_st_history(names, date.today())

    def benchmark(self, code: str, start: date, end: date) -> pd.DataFrame:
        if code == "H00300":
            raw = self._call(
                lambda: ak.stock_zh_index_hist_csindex(
                    symbol=code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
            )
            return normalize_csindex(code, raw)
        if code == "510880":
            # 总收益须从上市首日累计起算，因此总是取全历史再截取区间
            prices = self._call(lambda: ak.fund_etf_hist_sina(symbol="sh510880"))
            paid = self._call(lambda: ak.fund_etf_dividend_sina(symbol="sh510880"))
            return _slice_days(build_etf_total_return(code, prices, paid), start, end)
        raise ValueError(f"不支持的基准代码：{code}")
