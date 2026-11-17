"""网页进程的行情缓存：结果与全量构建逐项相等、只构建一次、完成日变化后重建、并发只建一份。"""

import threading
import time
from collections.abc import Collection
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig, load_config
from qt.data.store import ParquetStore
from qt.db import admin_repo
from qt.db.models import DailyRun
from qt.jobs.daily import FINISHED
from qt.service.accounts import create_subscription, create_user
from qt.service.actor import CLI
from qt.service.manual_ops import manual_state, record_holding
from qt.service.market import StoreMarket, build_store_market
from qt.web import market as web_market
from qt.web.market import MarketCache
from tests.fakes import TRADING_SYMBOLS, populate_trading_store

CFG: AppConfig = load_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
S = 260  # 订阅起始日的交易日序号


class World:
    """一份小夹具数据：存储、日历与全部交易日。"""

    def __init__(self, root: Path) -> None:
        self.store = ParquetStore(root)
        self.days = populate_trading_store(self.store)
        self.calendar = TradingCalendar(self.days)

    @property
    def today(self) -> date:
        return self.days[S + 30]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    return World(tmp_path_factory.mktemp("web_market"))


class Counter:
    """包一层构建函数，数它被调用了几次。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(
        self,
        store: ParquetStore,
        cfg: AppConfig,
        calendar: TradingCalendar,
        today: date,
        symbols: Collection[str] | None = None,
    ) -> StoreMarket:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return build_store_market(store, cfg, calendar, today, symbols)


def _cache(world: World, done: list[date | None]) -> MarketCache:
    return MarketCache(world.store, CFG, lambda: world.calendar, lambda: done[0])


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> Counter:
    c = Counter()
    monkeypatch.setattr(web_market, "build_store_market", c)
    return c


def test_manual_state_equals_full_build(db: Session, world: World) -> None:
    user = create_user(db, "cache@example.com", actor=CLI)
    sub = create_subscription(db, user.id, CFG, 100_000.0, world.days[S], None, actor=CLI)
    full = build_store_market(world.store, CFG, world.calendar, world.today)
    for symbol in TRADING_SYMBOLS[:2]:
        record_holding(
            db,
            user.id,
            sub.id,
            full,
            world.calendar,
            CFG,
            world.today,
            symbol,
            world.days[S],
            10.0,
            1000.0,
            actor=CLI,
        )
    db.commit()
    held = set(TRADING_SYMBOLS[:2])
    cached = _cache(world, [world.today]).get(world.today, held)
    expected = manual_state(db, user.id, sub.id, full, world.calendar, CFG, world.today)
    actual = manual_state(db, user.id, sub.id, cached, world.calendar, CFG, world.today)
    assert actual == expected
    assert actual.positions  # 确实有持仓，比较不是空对空


def test_symbols_none_matches_cli_behaviour(world: World) -> None:
    default = build_store_market(world.store, CFG, world.calendar, world.today)
    empty = build_store_market(world.store, CFG, world.calendar, world.today, ["600001"])
    day = world.today
    assert default.last_close(day, "600001") == empty.last_close(day, "600001")
    assert default.known_symbols() == empty.known_symbols()
    with pytest.raises(KeyError):
        empty.last_close(day, "600002")  # 面板只含指定的股票


def test_no_trading_day_raises(world: World) -> None:
    from qt.service.errors import ValidationError

    with pytest.raises(ValidationError):
        build_store_market(world.store, CFG, world.calendar, date(1980, 1, 1))


def test_second_get_reuses_market(world: World, counter: Counter) -> None:
    cache = _cache(world, [world.today])
    first = cache.get(world.today, {"600001"})
    second = cache.get(world.today, ["600001"])
    assert first is second
    assert counter.calls == 1


def test_symbol_order_and_duplicates_share_one_entry(world: World, counter: Counter) -> None:
    cache = _cache(world, [world.today])
    cache.get(world.today, ["600001", "600002"])
    cache.get(world.today, ("600002", "600001", "600001"))
    assert counter.calls == 1


def test_different_symbol_sets_build_separately(world: World, counter: Counter) -> None:
    cache = _cache(world, [world.today])
    cache.get(world.today, {"600001"})
    cache.get(world.today, {"600002"})
    cache.get(world.today, {"600001"})
    assert counter.calls == 2


def test_rebuild_after_latest_done_day_changes(world: World, counter: Counter) -> None:
    done: list[date | None] = [world.days[S]]
    cache = _cache(world, done)
    cache.get(world.today, {"600001"})
    cache.get(world.today, {"600001"})
    assert counter.calls == 1
    done[0] = world.days[S + 1]
    cache.get(world.today, {"600001"})
    assert counter.calls == 2
    done[0] = None
    cache.get(world.today, {"600001"})
    assert counter.calls == 3


def test_new_day_builds_again(world: World, counter: Counter) -> None:
    cache = _cache(world, [world.today])
    cache.get(world.today, {"600001"})
    cache.get(world.days[S + 31], {"600001"})
    assert counter.calls == 2


def test_keeps_a_bounded_number_and_drops_least_recently_used(
    world: World, counter: Counter
) -> None:
    cache = _cache(world, [world.today])
    limit = web_market.MAX_ENTRIES
    sets = [{f"6{i:05d}"} for i in range(limit + 2)]
    for symbols in sets[:limit]:
        cache.get(world.today, symbols)
    assert counter.calls == limit
    cache.get(world.today, sets[0])  # 刚用过：成为最近使用
    cache.get(world.today, sets[limit])  # 多出的一份：丢掉最久未用的 sets[1]
    assert counter.calls == limit + 1
    cache.get(world.today, sets[0])
    assert counter.calls == limit + 1
    cache.get(world.today, sets[1])
    assert counter.calls == limit + 2


def test_concurrent_first_gets_build_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    slow = Counter(delay=0.3)
    monkeypatch.setattr(web_market, "build_store_market", slow)
    cache = _cache(world, [world.today])
    barrier = threading.Barrier(2)
    results: list[StoreMarket] = []

    def work() -> None:
        barrier.wait()
        results.append(cache.get(world.today, {"600001"}))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert slow.calls == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_empty_symbol_set_builds_an_empty_panel(world: World) -> None:
    """账本里没有任何股票（新用户）也要能建出行情。"""
    market = build_store_market(world.store, CFG, world.calendar, world.today, [])
    assert "600001" in market.known_symbols()
    with pytest.raises(KeyError):
        market.last_close(world.today, "600001")


def test_cache_version_counts_done_with_errors_as_finished(db: Session) -> None:
    """缓存版本与补跑用同一个"已完成"集合：有使用者出错的那天也算完成。"""
    moment = datetime(2026, 11, 16, 19, 0, tzinfo=UTC)
    assert admin_repo.latest_done_day(db) is None
    for day, status in (
        (date(2026, 11, 12), "done"),
        (date(2026, 11, 13), "done_with_errors"),
        (date(2026, 11, 16), "data_failed"),
    ):
        db.add(DailyRun(trade_day=day, status=status, stage="x", started_at=moment, detail={}))
    db.flush()
    assert admin_repo.latest_done_day(db) == date(2026, 11, 13)
    assert set(FINISHED) == {"done", "done_with_errors"}
