# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportMissingTypeStubs=false
"""AKShare 数据源：带重试和间隔的网络封装。转换逻辑在 akshare_convert.py。"""

import threading
import time
import warnings
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date

import akshare as ak
import pandas as pd

from qt.config import AppConfig, DataConfig
from qt.data.akshare_convert import (
    build_etf_total_return,
    build_industries,
    classify_industry,
    normalize_bars,
    normalize_csindex,
    normalize_daily_snapshot,
    normalize_dividends,
    normalize_financials,
    normalize_securities,
    normalize_st_history,
    parse_disclosure,
    period_label,
    to_dates,
)
from qt.data.schema import Table, empty_frame, validate


class RequestTimeoutError(TimeoutError):
    """一次网络请求超过墙钟时限仍未返回。"""


def call_with_deadline[T](fn: Callable[[], T], timeout_seconds: float) -> T:
    """在后台守护线程里调用 fn，超过 timeout_seconds 秒未返回则抛 RequestTimeoutError。

    AKShare 的套接字超时只限制两次收到数据之间的间隔，部分接口还会执行没有超时的 JavaScript，
    因此需要自己的墙钟时限。超时的调用无法被强行停止：它所在的线程被放弃（守护线程，
    不阻止程序退出），返回值被丢弃。
    """
    future: Future[T] = Future()

    def worker() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn())
        except BaseException as exc:  # 原样交给调用方
            future.set_exception(exc)

    threading.Thread(target=worker, name="qt-request", daemon=True).start()
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        raise RequestTimeoutError(f"请求超过 {timeout_seconds:g} 秒仍未返回，已放弃") from None


def call_with_retry[T](
    fn: Callable[[], T],
    retries: int,
    pause_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    fatal: tuple[type[Exception], ...] = (),
    timeout_seconds: float | None = None,
) -> T:
    """调用 fn，失败则间隔后重试，最多尝试 retries 次；成功后也停顿 pause_seconds。

    fatal 中的异常类型（如数据格式变化）不重试，直接抛出；最后一次失败后不再等待。
    timeout_seconds 不为空时每次尝试都有墙钟时限（见 call_with_deadline），超时算一次失败的尝试。
    """
    attempts = max(retries, 1)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = fn() if timeout_seconds is None else call_with_deadline(fn, timeout_seconds)
        except fatal:
            raise
        except Exception as exc:  # 网络接口可能抛任何异常，统一重试
            last = exc
            if attempt < attempts:
                sleep(pause_seconds * attempt)
        else:
            sleep(pause_seconds)
            return result
    assert last is not None
    raise last


def _exchange_prefix(symbol: str) -> str:
    """新浪接口的代码前缀：上交所 sh，深交所 sz。"""
    return ("sh" if symbol.startswith("6") else "sz") + symbol


def _slice_days(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    mask = (df["day"] >= pd.Timestamp(start)) & (df["day"] <= pd.Timestamp(end))
    return df[mask].reset_index(drop=True)


class AkshareSource:
    """基于 AKShare（新浪、同花顺、巨潮、中证）的 DataSource。

    所有网络请求依次进行、带重试，每次请求之后停顿 request_pause_seconds；每次尝试都有
    request_timeout_seconds 的墙钟时限（在后台线程里执行，超时即放弃）。
    """

    def __init__(self, data: DataConfig, boards: Sequence[str], data_start: date) -> None:
        self._retries = data.retries
        self._pause = data.request_pause_seconds
        self._timeout = data.request_timeout_seconds
        setter = getattr(ak, "set_timeout", None)
        if callable(setter):
            # AKShare 自带的是套接字超时（连接、两次收到数据的间隔），只作补充，不能依赖
            setter((10, self._timeout))
        self._boards = list(boards)
        self._data_start = data_start
        self._as_of: date | None = None
        self._disclosure: dict[date, dict[str, date]] = {}

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "AkshareSource":
        """按应用配置创建。"""
        return cls(cfg.data, cfg.universe.boards, cfg.backtest.data_start)

    def set_as_of(self, as_of: date) -> None:
        """设定本次运行的“最近已收盘交易日”；不设定时取系统日期。"""
        self._as_of = as_of

    @property
    def as_of(self) -> date:
        return self._as_of if self._as_of is not None else date.today()

    def _call[T](self, fn: Callable[[], T], fatal: tuple[type[Exception], ...] = ()) -> T:
        return call_with_retry(
            fn, self._retries, self._pause, fatal=fatal, timeout_seconds=self._timeout
        )

    def trade_days(self) -> pd.DataFrame:
        raw = self._call(ak.tool_trade_date_hist_sina)
        out = pd.DataFrame({"day": to_dates(raw["trade_date"])}).dropna()
        out = out.drop_duplicates().sort_values("day").reset_index(drop=True)
        validate(Table.TRADE_DAYS, out)
        return out

    def securities(self) -> pd.DataFrame:
        sh = self._call(lambda: ak.stock_info_sh_name_code(symbol="主板A股"))
        sz = self._call(lambda: ak.stock_info_sz_name_code(symbol="A股列表"))
        sh_d = self._call(lambda: ak.stock_info_sh_delist(symbol="全部"))
        sz_d = self._call(lambda: ak.stock_info_sz_delist(symbol="终止上市公司"))
        return normalize_securities(sh, sz, sh_d, sz_d, self._boards)

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

    def daily_snapshot(self) -> pd.DataFrame:
        """新浪全市场行情（约 5500 只，约 20 秒）。

        只尝试一次、不重试（新浪可能限流重复的批量请求），仍有墙钟时限；
        失败的异常原样抛出，由调用方退回逐只请求。
        """
        raw = call_with_retry(ak.stock_zh_a_spot, 1, self._pause, timeout_seconds=self._timeout)
        return normalize_daily_snapshot(raw)

    def dividends(self, symbol: str) -> pd.DataFrame:
        raw = self._call(lambda: ak.stock_history_dividend_detail(symbol=symbol, indicator="分红"))
        out = normalize_dividends(symbol, raw)
        return out[out["ex_date"] >= pd.Timestamp(self._data_start)].reset_index(drop=True)

    def _disclosure_dates(self, period: date) -> dict[str, date]:
        """巨潮某报告期的 {代码: 实际披露日}，按报告期缓存；尚未到期的报告期不请求。"""
        if period not in self._disclosure:
            label = period_label(period)
            if label is None or period > self.as_of:
                self._disclosure[period] = {}
            else:
                raw = self._call(lambda: ak.stock_report_disclosure(market="沪深京", period=label))
                self._disclosure[period] = parse_disclosure(raw)
        return self._disclosure[period]

    def financials(self, symbol: str) -> pd.DataFrame:
        raw = self._call(
            lambda: ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
        )
        periods = to_dates(raw["报告期"]).dropna()
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

    def industry(self, symbol: str) -> tuple[str, bool] | None:
        """某只在市股票的巨潮行业；返回 None 表示表为空（没有分类）。

        接口格式变化（KeyError、缺列）不重试也不吞掉，由调用方记为该股票的失败。
        """
        end = self.as_of.strftime("%Y%m%d")
        raw = self._call(
            lambda: ak.stock_industry_change_cninfo(
                symbol=symbol, start_date="19900101", end_date=end
            ),
            fatal=(KeyError,),
        )
        return classify_industry(raw)

    def industries(self) -> pd.DataFrame:
        """整表查询：逐只查询全部在市股票（增量更新请用 industry()）；任何失败都抛出。"""
        secs = self.securities()
        found: dict[str, tuple[str, bool]] = {}
        for symbol in secs[secs["delist_date"].isna()]["symbol"]:
            classified = self.industry(str(symbol))
            if classified is not None:
                found[str(symbol)] = classified
        return build_industries(found)

    def st_history(self) -> pd.DataFrame:
        names = self._call(ak.stock_info_a_code_name)
        return normalize_st_history(names, self.as_of)

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
