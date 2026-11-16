"""各页面需要的只读查询：网页与命令行都从这里取数据，保证两边显示同一份数据。

全部函数形如 (s, user_id, …)，订阅编号一律按该使用者校验归属（不是本人的抛 NotFoundError）；
返回冻结的 dataclass，不返回数据库对象（resolve_subscription 除外，它给路由取订阅用）。
latest_run、recent_runs 是全站的运行记录，不属于某个使用者。
"""

from qt.service.views.base import (
    BOOK_LABELS,
    BOOKS,
    EXIT_LABELS,
    FOLLOWUP_LABELS,
    KIND_LABELS,
    RUN_STATUS_LABELS,
    SIDE_LABELS,
    SIGNAL_STATUS_LABELS,
    SUB_STATUS_LABELS,
    BookKind,
    Names,
    SubscriptionView,
    origin_label,
    resolve_subscription,
    subscription_view,
    subscriptions,
)
from qt.service.views.books import (
    BookSummary,
    EntryRow,
    HoldingRow,
    ManualBookView,
    NavPoint,
    SystemBookView,
    book_symbols,
    manual_book,
    system_book,
)
from qt.service.views.journal_list import (
    BookJournal,
    FollowupRow,
    JournalView,
    TradeRow,
    book_journal,
    journal,
)
from qt.service.views.overview_page import OverviewView, overview
from qt.service.views.runs import RunDetail, RunsView, RunView, latest_run, recent_runs
from qt.service.views.signal_list import (
    RejectionRow,
    SignalRow,
    SignalsView,
    signal_rows,
    signals_page,
)

__all__ = [
    "BOOKS",
    "BOOK_LABELS",
    "EXIT_LABELS",
    "FOLLOWUP_LABELS",
    "KIND_LABELS",
    "RUN_STATUS_LABELS",
    "SIDE_LABELS",
    "SIGNAL_STATUS_LABELS",
    "SUB_STATUS_LABELS",
    "BookJournal",
    "BookKind",
    "BookSummary",
    "EntryRow",
    "FollowupRow",
    "HoldingRow",
    "JournalView",
    "ManualBookView",
    "Names",
    "NavPoint",
    "OverviewView",
    "RejectionRow",
    "RunDetail",
    "RunView",
    "RunsView",
    "SignalRow",
    "SignalsView",
    "SubscriptionView",
    "SystemBookView",
    "TradeRow",
    "book_journal",
    "book_symbols",
    "journal",
    "latest_run",
    "manual_book",
    "origin_label",
    "overview",
    "recent_runs",
    "resolve_subscription",
    "signal_rows",
    "signals_page",
    "subscription_view",
    "subscriptions",
    "system_book",
]
