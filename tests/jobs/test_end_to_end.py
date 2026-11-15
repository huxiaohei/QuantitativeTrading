"""端到端：两位使用者逐日补跑 30 个交易日，甲照信号录入、乙从不录入，检查口径一致且互不串户。"""

import re
from collections import defaultdict
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qt.backtest.report import run_period
from qt.config import AppConfig
from qt.data.schema import Table
from qt.data.store import ParquetStore
from qt.db import repo
from qt.db.models import BookDay, Followup, Outbox, Signal, Subscription
from qt.features import build_panel
from qt.jobs.daily import catch_up
from qt.models import Side
from qt.service.accounts import create_subscription, create_user, subscription_config
from qt.service.manual_ops import manual_state, record_trade
from qt.service.market import StoreMarket
from tests.jobs.conftest import CFG, DAY, Env, evening

DAYS = 30
CASH_A = 150000.0
CASH_B = 230000.0  # 与甲不同，股数与金额才能区分两人的邮件


def _subscribe(
    db: Session, email: str, start: date, cash: float, overrides: dict[str, object] | None = None
) -> Subscription:
    user = create_user(db, email)
    sub = create_subscription(db, user.id, CFG, cash, start, overrides)
    db.commit()
    return sub


def _equity(db: Session, sub: Subscription, kind: str) -> list[float]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, kind)
    rows = db.scalars(select(BookDay).where(BookDay.book_id == book.id).order_by(BookDay.day))
    return [r.equity for r in rows]


def _close(store: ParquetStore, symbol: str, day: date) -> float:
    bars = store.read(Table.BARS, symbols=[symbol])
    return float(bars.loc[bars["day"] == pd.Timestamp(day), "close"].iloc[0])


def _signals(db: Session, sub: Subscription, kind: str) -> list[Signal]:
    db.rollback()
    book = repo.get_book_of_kind(db, sub.user_id, sub.id, kind)
    return repo.list_signals(db, sub.user_id, book.id)


def _market(env: Env, day: date) -> StoreMarket:
    return StoreMarket(env.store, build_panel(env.store, env.calendar, CFG, day, day), CFG)


def _record_open_buys(env: Env, db: Session, sub: Subscription, yesterday: date, day: date) -> int:
    """甲在收到买入信号的次日，按信号股数、当日收盘价录入成交。"""
    market = _market(env, day)
    cfg = subscription_config(CFG, sub)
    done = 0
    for sig in _signals(db, sub, "manual"):
        if sig.side != "buy" or sig.signal_day != yesterday or sig.status != "notified":
            continue
        price = _close(env.store, sig.symbol, day)
        args = (db, sub.user_id, sub.id, market, env.calendar, cfg, day)
        record_trade(*args, Side.BUY, sig.symbol, day, price, sig.shares)
        done += 1
    db.commit()
    return done


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,]*(?:\.\d+)?", text))


def _with_cash(cfg: AppConfig, cash: float) -> AppConfig:
    backtest = cfg.backtest.model_copy(update={"initial_cash": cash})
    return cfg.model_copy(update={"backtest": backtest})


def test_two_users_thirty_days_end_to_end(env: Env, db: Session) -> None:
    days = env.days
    start = days[DAY]
    a = _subscribe(db, "a@example.com", start, CASH_A)
    # 乙：止损 10%、最多持有 2 只；本金不同
    b = _subscribe(
        db, "b@example.com", start, CASH_B, {"strategy.stop_loss": 0.10, "risk.max_positions": 2}
    )

    recorded = 0
    for i in range(DAYS):
        day = days[DAY + i]
        if i > 0:
            recorded += _record_open_buys(env, db, a, days[DAY + i - 1], day)
        env.clock.now = evening(day)
        assert catch_up(env.ctx) == [(day, "done")]
    last = days[DAY + DAYS - 1]
    assert recorded > 0

    # 1. 两人的系统账分别与各自参数下的 run_period 完全相同
    for sub, cash in ((a, CASH_A), (b, CASH_B)):
        db.rollback()
        fresh = repo.get_subscription(db, sub.user_id, sub.id)
        whole = run_period(
            _with_cash(subscription_config(CFG, fresh), cash), env.store, start, last
        )
        equity = _equity(db, sub, "system")
        assert len(equity) == DAYS
        assert equity == pytest.approx(list(whole.equity), rel=0, abs=0)
        assert [x.hex() for x in equity] == [float(x).hex() for x in whole.equity]
        assert whole.equity.iloc[-1] != whole.equity.iloc[0]  # 期间确有成交

    # 2. 甲：手动账有持仓，买入信号全部已执行并关联到成交
    db.rollback()
    state = manual_state(db, a.user_id, a.id, _market(env, last), env.calendar, CFG, last)
    assert state.positions
    a_buys = [s for s in _signals(db, a, "manual") if s.side == "buy"]
    assert a_buys and all(s.status == "executed" for s in a_buys)
    a_book = repo.get_book_of_kind(db, a.user_id, a.id, "manual")
    linked = {e.signal_id for e in repo.list_ledger_entries(db, a.user_id, a_book.id)}
    assert {s.id for s in a_buys} <= linked

    # 3. 乙：从不录入，买入信号全部放弃，每个都有一条放弃跟踪记录
    b_buys = [s for s in _signals(db, b, "manual") if s.side == "buy"]
    # 最后一晚刚发出的信号还在有效期内，其余全部到期放弃
    assert b_buys and len(b_buys) > 2
    expired = [s for s in b_buys if s.status == "abandoned"]
    still_open = [s for s in b_buys if s.status != "abandoned"]
    assert expired and all(s.signal_day == last and s.status == "notified" for s in still_open)
    b_book = repo.get_book_of_kind(db, b.user_id, b.id, "manual")
    followups = db.scalars(
        select(Followup).where(Followup.book_id == b_book.id, Followup.event == "abandoned")
    ).all()
    assert sorted((f.symbol, f.event_day) for f in followups) == sorted(
        (s.symbol, s.status_day) for s in expired
    )

    # 4. 邮件不串户：按收件人分组，对方的股数与本金不出现
    rows = list(db.scalars(select(Outbox).order_by(Outbox.id)))
    by_addr: dict[str, list[Outbox]] = defaultdict(list)
    for row in rows:
        by_addr[row.to_addr].append(row)
    assert {"a@example.com", "b@example.com"} <= set(by_addr)
    own = {
        "a@example.com": {f"{s.shares:.0f}" for s in _signals(db, a, "manual")},
        "b@example.com": {f"{s.shares:.0f}" for s in _signals(db, b, "manual")},
    }
    own_cash = {
        "a@example.com": (f"{CASH_A:.0f}", f"{CASH_A:,.0f}"),
        "b@example.com": (f"{CASH_B:.0f}", f"{CASH_B:,.0f}"),
    }
    for addr, other in (("a@example.com", "b@example.com"), ("b@example.com", "a@example.com")):
        foreign = own[other] - own[addr]
        assert foreign  # 两人的股数确有区别，检查才有意义
        seen: set[str] = set()
        for mail in by_addr[addr]:
            everything = "\n".join((mail.subject, mail.body_text, mail.body_html))
            seen |= _numbers(mail.subject + "\n" + mail.body_text)
            assert other not in everything
            assert all(c not in everything for c in own_cash[other])
        assert not foreign & seen
        assert own[addr] & seen  # 自己的股数确实写在邮件里

    # 5. 发件箱：没有重复键，也没有 pending 之外的异常状态（这里全部已发出）
    keys = [r.dedupe_key for r in rows]
    assert len(keys) == len(set(keys))
    assert {r.status for r in rows} == {"sent"}
    assert len(env.sender.sent) == len(rows)
