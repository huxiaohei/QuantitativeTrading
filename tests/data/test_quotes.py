"""盘中报价：腾讯返回文本的解析、交易时段判断、共用缓存。"""

import threading
import urllib.request
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from qt.calendar import TradingCalendar
from qt.data import quotes as quotes_module
from qt.data.quotes import (
    LivePlan,
    LiveQuote,
    NoRedirect,
    QuoteCache,
    exchange_prefix,
    fetch_tencent,
    in_session,
    live_plan,
    parse_tencent,
    same_day,
)

T0 = datetime(2026, 11, 17, 10, 0, 0)


def _line(code: str, symbol: str, price: str, prev: str, stamp: str = "20261117100003") -> str:
    fields = ["1", "名称", symbol, price, prev] + ["0"] * 25 + [stamp, "0", "0"]
    return f'v_{code}="{"~".join(fields)}";'


@pytest.mark.parametrize(
    ("symbol", "prefix"),
    [
        ("600519", "sh"),
        ("900901", "sh"),
        ("000001", "sz"),
        ("200002", "sz"),
        ("300750", "sz"),
        ("430047", "bj"),
        ("830799", "bj"),
        ("100000", None),
        ("60051", None),
        ("6005190", None),
        ("abcdef", None),
        ("\uff16\uff10\uff10\uff15\uff11\uff19", None),
    ],
)
def test_exchange_prefix(symbol: str, prefix: str | None) -> None:
    assert exchange_prefix(symbol) == prefix


def test_parse_tencent_reads_price_prev_close_and_time() -> None:
    text = (
        _line("sh600519", "600519", "1439.78", "1453.90")
        + "\n"
        + _line("sz000001", "000001", "0.00", "12.30")
    )
    quotes = parse_tencent(text)
    assert quotes == {
        "600519": LiveQuote(1439.78, 1453.90, datetime(2026, 11, 17, 10, 0, 3)),
        "000001": LiveQuote(0.0, 12.30, datetime(2026, 11, 17, 10, 0, 3)),
    }


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v_pv_none_match=1;",
        'v_sh600519="1~名称~600519~1439.78";',
        _line("sh600519", "600519", "abc", "1453.90"),
        _line("sh600519", "600519", "nan", "1453.90"),
        _line("sh600519", "600519", "-1", "1453.90"),
        _line("sh600519", "600519", "inf", "1453.90"),
        _line("sh600519", "600519", "1439.78", "1453.90", stamp="2026-11-17"),
        _line("sh600519", "<script>", "1439.78", "1453.90"),
    ],
)
def test_parse_tencent_drops_bad_lines_and_keeps_good_ones(bad: str) -> None:
    quotes = parse_tencent(bad + _line("sz000001", "000001", "12.17", "12.30"))
    assert list(quotes) == ["000001"]


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("09:29:59", False),
        ("09:30:00", True),
        ("11:30:00", True),
        ("11:30:01", False),
        ("12:59:59", False),
        ("13:00:00", True),
        ("15:00:00", True),
        ("15:00:01", False),
    ],
)
def test_in_session_on_a_trading_day(clock: str, expected: bool) -> None:
    calendar = TradingCalendar([date(2026, 11, 17)])
    now = datetime.fromisoformat(f"2026-11-17T{clock}")
    assert in_session(now, calendar) is expected


def test_in_session_is_false_on_a_non_trading_day() -> None:
    calendar = TradingCalendar([date(2026, 11, 17)])
    assert in_session(datetime(2026, 11, 21, 10, 0), calendar) is False


class FakeFetch:
    def __init__(self) -> None:
        self.calls: list[set[str]] = []
        self.price = 10.0
        self.fail = False
        self.missing: set[str] = set()

    def __call__(self, symbols: Collection[str]) -> dict[str, LiveQuote]:
        self.calls.append(set(symbols))
        if self.fail:
            raise TimeoutError("timed out")
        found = [symbol for symbol in symbols if symbol not in self.missing]
        return {symbol: LiveQuote(self.price, 9.0, T0) for symbol in found}


def test_cache_is_shared_within_ttl_and_refetches_only_stale_symbols() -> None:
    fetch = FakeFetch()
    cache = QuoteCache(fetch, ttl_seconds=10)
    assert cache.get(["600519"], T0)["600519"].price == 10.0
    assert cache.get(["600519", "000001"], T0 + timedelta(seconds=5)).keys() == {"600519", "000001"}
    assert fetch.calls == [{"600519"}, {"000001"}]
    # 600519 在第 10 秒过期，000001（第 5 秒取的）还没有
    fetch.price = 11.0
    got = cache.get(["600519", "000001"], T0 + timedelta(seconds=10))
    assert fetch.calls[-1] == {"600519"}
    assert (got["600519"].price, got["000001"].price) == (11.0, 10.0)


def test_cache_keeps_old_quote_when_fetch_fails(caplog: pytest.LogCaptureFixture) -> None:
    fetch = FakeFetch()
    cache = QuoteCache(fetch, ttl_seconds=10)
    cache.get(["600519"], T0)
    fetch.fail = True
    with caplog.at_level("WARNING"):
        assert cache.get(["600519"], T0 + timedelta(seconds=20))["600519"].price == 10.0
        assert cache.get(["600519"], T0 + timedelta(seconds=40))["600519"].price == 10.0
        cache.get(["600519"], T0 + timedelta(seconds=90))
    # 同类失败一分钟最多记一条：第 20、40 秒合一条，第 90 秒再一条
    assert len(caplog.records) == 2
    assert "TimeoutError" in caplog.text


def test_cache_returns_nothing_when_fetch_fails_and_no_old_quote() -> None:
    fetch = FakeFetch()
    fetch.fail = True
    assert QuoteCache(fetch).get(["600519"], T0) == {}


def test_cache_omits_symbols_the_source_does_not_return() -> None:
    fetch = FakeFetch()
    fetch.missing = {"000001"}
    assert QuoteCache(fetch).get(["600519", "000001"], T0).keys() == {"600519"}


def test_cache_drops_an_old_quote_once_the_source_stops_returning_the_symbol() -> None:
    fetch = FakeFetch()
    cache = QuoteCache(fetch, ttl_seconds=10)
    assert cache.get(["600519"], T0).keys() == {"600519"}
    fetch.missing = {"600519"}
    assert cache.get(["600519"], T0 + timedelta(seconds=20)) == {}


def test_cache_does_not_wait_for_a_fetch_in_flight() -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[set[str]] = []

    def slow(symbols: Collection[str]) -> dict[str, LiveQuote]:
        calls.append(set(symbols))
        started.set()
        release.wait(timeout=5)
        return {symbol: LiveQuote(10.0, 9.0, T0) for symbol in symbols}

    cache = QuoteCache(slow)
    worker = threading.Thread(target=cache.get, args=(["600519"], T0))
    worker.start()
    assert started.wait(timeout=5)
    # 另一个请求此时发现缓存没有：不等待、不再发请求，直接得到空结果
    assert cache.get(["600519"], T0) == {}
    release.set()
    worker.join(timeout=5)
    assert calls == [{"600519"}]
    assert cache.get(["600519"], T0)["600519"].price == 10.0


def test_cache_size_is_bounded_and_drops_the_oldest() -> None:
    fetch = FakeFetch()
    cache = QuoteCache(fetch, ttl_seconds=1000, max_size=2)
    cache.get(["600001"], T0)
    cache.get(["600002"], T0 + timedelta(seconds=1))
    cache.get(["600003"], T0 + timedelta(seconds=2))
    fetch.fail = True
    later = T0 + timedelta(seconds=3)
    assert cache.get(["600001", "600002", "600003"], later).keys() == {"600002", "600003"}


CAL = TradingCalendar([date(2026, 11, 17)])


@pytest.mark.parametrize(
    ("clock", "plan"),
    [
        ("07:00:00", LivePlan(False, 600)),  # 离开盘还早：最长 10 分钟来一次
        ("09:25:00", LivePlan(False, 300)),  # 等到 09:30
        ("09:29:55", LivePlan(False, 15)),  # 不短于 15 秒
        ("09:30:00", LivePlan(True, 15)),
        ("11:30:00", LivePlan(True, 15)),
        ("11:30:01", LivePlan(True, 600)),  # 午间休市：仍显示上午最后的报价，等到 13:00
        ("12:58:00", LivePlan(True, 120)),
        ("13:00:00", LivePlan(True, 15)),
        ("15:00:00", LivePlan(True, 15)),
        ("15:00:01", LivePlan(False, None)),  # 收盘后不再取
        ("18:30:00", LivePlan(False, None)),
    ],
)
def test_live_plan_on_a_trading_day(clock: str, plan: LivePlan) -> None:
    assert live_plan(datetime.fromisoformat(f"2026-11-17T{clock}"), CAL) == plan


def test_live_plan_on_a_non_trading_day() -> None:
    assert live_plan(datetime(2026, 11, 21, 10, 0), CAL) == LivePlan(False, None)


def test_session_and_plan_convert_aware_times_to_shanghai() -> None:
    utc = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)  # 上海 10:00
    shanghai = datetime(2026, 11, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    for now in (utc, shanghai):
        assert in_session(now, CAL) is True
        assert live_plan(now, CAL) == LivePlan(True, 15)
    late_utc = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # 上海 17 日 02:00
    assert live_plan(late_utc, CAL) == LivePlan(False, 600)


def test_same_day_drops_quotes_from_another_day() -> None:
    now = datetime(2026, 11, 17, 9, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    quotes = {
        "600519": LiveQuote(10.0, 9.0, datetime(2026, 11, 16, 14, 59, 58)),
        "000001": LiveQuote(10.0, 9.0, datetime(2026, 11, 17, 9, 30, 3)),
    }
    assert same_day(quotes, now).keys() == {"000001"}


def test_fetch_tencent_batches_decodes_gbk_and_skips_unknown_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def fake_read(url: str) -> bytes:
        urls.append(url)
        codes = url.split("q=")[1].split(",")
        return "\n".join(_line(c, c[2:], "12.17", "12.30") for c in codes).encode("gbk")

    monkeypatch.setattr(quotes_module, "read_url", fake_read)
    symbols = [f"600{i:03d}" for i in range(61)] + ["000001", "123456", "abc"]
    got = fetch_tencent(symbols)
    assert len(urls) == 2 and all(u.startswith("https://qt.gtimg.cn/q=") for u in urls)
    assert len(urls[0].split(",")) == 60 and len(urls[1].split(",")) == 2
    assert "sz000001" in urls[1] and "123456" not in "".join(urls)
    assert len(got) == 62 and got["000001"].price == 12.17


def test_fetch_tencent_does_not_follow_redirects() -> None:
    handler = NoRedirect()
    request = urllib.request.Request("https://qt.gtimg.cn/q=sh600519")
    assert handler.redirect_request(request, None, 302, "Found", None, "http://10.0.0.1/") is None


def test_cache_turns_an_http_error_into_old_values() -> None:
    def failing(symbols: Collection[str]) -> dict[str, LiveQuote]:
        del symbols
        raise HTTPError("https://qt.gtimg.cn/q=", 502, "Bad Gateway", Message(), None)

    assert QuoteCache(failing).get(["600519"], T0) == {}
