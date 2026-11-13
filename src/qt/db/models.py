"""全部表结构（SQLAlchemy 2.0 声明式）。

约定：
- 金额、份额、价格一律 double precision，与引擎中的浮点数逐位一致。
- 有顺序的集合放进 JSONB 时用数组，不依赖对象键的顺序。
- 时间戳都带时区；业务日期用 date（Asia/Shanghai 的交易日）。
- 使用者名下的表都有 user_id；跨使用者的查询只出现在 admin_repo。
后续任务只增加迁移，不改变这里已有列的含义。
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有表的基类。"""


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    joined = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} in ({joined})", name=name)


class User(Base):
    """使用者。邮箱一律小写存放并唯一。"""

    __tablename__ = "users"
    __table_args__ = (_one_of("status", ("active", "disabled"), "ck_users_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    plan: Mapped[str] = mapped_column(String(32), server_default="free")
    weekly_mail: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_version: Mapped[str | None] = mapped_column(String(32))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Subscription(Base):
    """策略订阅。params 形如 {"strategy": {...}, "risk": {...}}。"""

    __tablename__ = "subscriptions"
    __table_args__ = (
        _one_of("status", ("active", "paused", "closed"), "ck_subscriptions_status"),
        # 每个使用者同时只能有一个未关闭的订阅
        Index(
            "uq_subscriptions_one_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    initial_cash: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    start_day: Mapped[date] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ParamChange(Base):
    """参数修改历史：old/new 为完整参数快照。"""

    __tablename__ = "param_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    old: Mapped[dict[str, Any]] = mapped_column(JSONB)
    new: Mapped[dict[str, Any]] = mapped_column(JSONB)
    effective_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()


class Book(Base):
    """账本：每个订阅一本系统账（system）、一本手动账（manual）。"""

    __tablename__ = "books"
    __table_args__ = (
        UniqueConstraint("subscription_id", "kind", name="uq_books_subscription_kind"),
        _one_of("kind", ("system", "manual"), "ck_books_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    processed_through: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()


class SystemState(Base):
    """系统账的引擎状态快照（现金、持仓、挂单等）。"""

    __tablename__ = "system_states"

    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    as_of_day: Mapped[date] = mapped_column(Date)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class BookDay(Base):
    """账本每日净值。"""

    __tablename__ = "book_days"

    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    equity: Mapped[float] = mapped_column(Double)
    cash: Mapped[float] = mapped_column(Double)
    unit_nav: Mapped[float] = mapped_column(Double)
    created_at: Mapped[datetime] = _created_at()


class LedgerEntry(Base):
    """手动账流水：只追加；更正用 void 条目冲销。"""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        _one_of(
            "kind",
            ("buy", "sell", "transfer_in", "deposit", "withdraw", "void"),
            "ck_ledger_entries_kind",
        ),
        _one_of("origin", ("signal", "self"), "ck_ledger_entries_origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(16))
    trade_day: Mapped[date] = mapped_column(Date)
    effective_day: Mapped[date] = mapped_column(Date)
    shares: Mapped[float] = mapped_column(Double)
    price: Mapped[float] = mapped_column(Double)
    fees: Mapped[float] = mapped_column(Double)
    fees_estimated: Mapped[bool] = mapped_column(Boolean)
    cost_price: Mapped[float | None] = mapped_column(Double)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    origin: Mapped[str | None] = mapped_column(String(16))
    voids_entry_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_entries.id"))
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = _created_at()


class Signal(Base):
    """交易信号。"""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("book_id", "side", "symbol", "signal_day", name="uq_signals_natural_key"),
        _one_of(
            "status",
            ("created", "notified", "executed", "abandoned", "voided", "unnotified"),
            "ck_signals_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    side: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(16))
    signal_day: Mapped[date] = mapped_column(Date)
    shares: Mapped[float] = mapped_column(Double)
    ref_price: Mapped[float] = mapped_column(Double)
    reason: Mapped[str] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    stop_price: Mapped[float | None] = mapped_column(Double)
    take_price: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(16), server_default="created")
    status_day: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    reminded_day: Mapped[date | None] = mapped_column(Date)
    void_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Rejection(Base):
    """被风控或规则拒绝的候选；同一只股票相隔不超过间隔天数的拒绝合并为一个事件。"""

    __tablename__ = "rejections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    first_day: Mapped[date] = mapped_column(Date)
    last_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()


class RiskEvent(Base):
    """回撤预警与暂停事件。"""

    __tablename__ = "risk_events"
    __table_args__ = (UniqueConstraint("book_id", "day", "kind", name="uq_risk_events_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(32))
    drawdown: Mapped[float] = mapped_column(Double)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Trade(Base):
    """已完成的一笔完整交易（买入到卖出）。"""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    entry_day: Mapped[date] = mapped_column(Date)
    exit_day: Mapped[date] = mapped_column(Date)
    shares: Mapped[float] = mapped_column(Double)
    cost: Mapped[float] = mapped_column(Double)
    proceeds: Mapped[float] = mapped_column(Double)
    cash_received: Mapped[float] = mapped_column(Double)
    exit_reason: Mapped[str] = mapped_column(String(32))  # 手动卖出且无关联信号时为 manual
    holding_days: Mapped[int] = mapped_column(Integer)
    entry_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(16))
    signal_ref_price: Mapped[float | None] = mapped_column(Double)
    fill_price: Mapped[float] = mapped_column(Double)
    created_at: Mapped[datetime] = _created_at()


class Followup(Base):
    """离场、放弃、被拒之后的收益跟踪。"""

    __tablename__ = "followups"
    __table_args__ = (
        UniqueConstraint("book_id", "event", "symbol", "event_day", name="uq_followups_key"),
        _one_of("event", ("exit", "abandoned", "rejected"), "ck_followups_event"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(16))
    event_day: Mapped[date] = mapped_column(Date)
    base_adj_close: Mapped[float] = mapped_column(Double)
    ret_20: Mapped[float | None] = mapped_column(Double)
    ret_60: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[datetime] = _created_at()


class Outbox(Base):
    """待发邮件：dedupe_key 唯一，保证同一封邮件不重复入队。"""

    __tablename__ = "outbox"
    __table_args__ = (_one_of("status", ("pending", "sent", "failed"), "ck_outbox_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    to_addr: Mapped[str] = mapped_column(String(320))
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    subject: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class DailyRun(Base):
    """每日运行记录，以交易日为主键。"""

    __tablename__ = "daily_runs"
    __table_args__ = (
        _one_of(
            "status",
            ("running", "data_failed", "done", "done_with_errors"),
            "ck_daily_runs_status",
        ),
    )

    trade_day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(24))
    stage: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class AuditLog(Base):
    """审计日志：改动使用者数据的操作都记一条。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
