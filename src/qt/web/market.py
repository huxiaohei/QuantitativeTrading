"""网页进程的行情缓存。

全量指标面板在真实数据上要占几个 GB 内存（见任务报告的测量），服务器放不下，
所以按"当前账本涉及的股票集合"缓存：键是（日期，股票集合），最多保留 24 份，最久未用的先丢
（持仓表在交易时段定时刷新，每个打开的页面都会反复用到自己那一份；一份只覆盖一本账的几只股票）。
构建在锁内进行，同时到来的请求等待同一次构建而不是各建一份。
"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from datetime import date

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.data.store import ParquetStore
from qt.service.market import StoreMarket, build_store_market

MAX_ENTRIES = 24


class MarketCache:
    """按股票集合缓存的行情；每日任务完成日（latest_done_day）变化后全部作废。"""

    def __init__(
        self,
        store: ParquetStore,
        cfg: AppConfig,
        calendar_factory: Callable[[], TradingCalendar],
        latest_done_day: Callable[[], date | None],
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._calendar_factory = calendar_factory
        self._latest_done_day = latest_done_day
        self._lock = threading.Lock()
        self._version: date | None = None
        self._entries: OrderedDict[tuple[date, frozenset[str]], StoreMarket] = OrderedDict()

    def get(self, today: date, symbols: Collection[str]) -> StoreMarket:
        """今天的行情，面板只覆盖 symbols；已有同样的一份就直接用。"""
        key = (today, frozenset(symbols))
        with self._lock:
            version = self._latest_done_day()
            if version != self._version:
                self._entries.clear()
                self._version = version
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                return found
            built = build_store_market(
                self._store, self._cfg, self._calendar_factory(), today, key[1]
            )
            self._entries[key] = built
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
            return built
