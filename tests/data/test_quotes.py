"""盘中报价：腾讯返回文本的解析、交易时段判断、共用缓存。"""

import threading
from collections.abc import Collection
from datetime import date, datetime, timedelta

import pytest

from qt.calendar import TradingCalendar
from qt.data.quotes import LiveQuote, QuoteCache, exchange_prefix, in_session, parse_tencent

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
