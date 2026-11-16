"""总览页：最近一次运行、当前订阅、两本账的摘要、最近一次运行出的信号。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from qt.calendar import TradingCalendar
from qt.config import AppConfig
from qt.service.manual_book import Market
from qt.service.views.base import (
    BOOKS,
    Names,
    SubscriptionView,
    resolve_subscription,
    subscription_view,
)
from qt.service.views.books import BookSummary, manual_page, system_book
from qt.service.views.runs import RunView, latest_run
from qt.service.views.signal_list import SignalRow, signal_rows


@dataclass(frozen=True)
class OverviewView:
    """总览。没有订阅时 subscription、manual、system 为 None；系统账还没推进过时 system 为 None。

    todays_signals：最近一次运行（数据正常时）当天出的信号，手动账在前，各自编号从新到旧。
    manual_error：手动账流水不一致时的原因（此时 manual 为 None）。
    """

    run: RunView | None
    subscription: SubscriptionView | None
    manual: BookSummary | None
    system: BookSummary | None
    todays_signals: list[SignalRow]
    manual_error: str | None = None


def overview(
    s: Session,
    user_id: int,
    market: Market | None,
    calendar: TradingCalendar,
    cfg: AppConfig,
    today: date,
    *,
    subscription_id: int | None = None,
    names: Names | None = None,
) -> OverviewView:
    """总览。subscription_id 为 None 时看当前订阅（rules.shown_subscription），给了就必须是本人的。

    market 只在该使用者没有任何订阅时可以为 None。
    """
    run = latest_run(s)
    sub = resolve_subscription(s, user_id, subscription_id)
    if sub is None:
        return OverviewView(run, None, None, None, [])
    if market is None:
        raise ValueError("有订阅时总览需要行情")
    manual = manual_page(s, user_id, sub.id, market, calendar, cfg, today, names=names)
    system = system_book(s, user_id, sub.id, names=names)
    todays: list[SignalRow] = []
    if run is not None and run.data_ok:
        for book in BOOKS:
            rows = signal_rows(
                s, user_id, sub.id, book, calendar, cfg, today, since=run.day, names=names
            )
            todays += [r for r in rows if r.signal_day == run.day]
    return OverviewView(
        run, subscription_view(sub), manual.summary, system.summary, todays, manual.error
    )
