"""核心表：使用者、订阅、账本、信号、流水、发件箱、每日运行、审计。

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_runs",
        sa.Column("trade_day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('running', 'data_failed', 'done', 'done_with_errors')",
            name="ck_daily_runs_status",
        ),
        sa.PrimaryKeyConstraint("trade_day"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("plan", sa.String(length=32), server_default="free", nullable=False),
        sa.Column("weekly_mail", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_version", sa.String(length=32), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status in ('active', 'disabled')", name="ck_users_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_audit_log_actor_user_id"), "audit_log", ["actor_user_id"], unique=False
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("to_addr", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status in ('pending', 'sent', 'failed')", name="ck_outbox_status"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(op.f("ix_outbox_user_id"), "outbox", ["user_id"], unique=False)
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("initial_cash", sa.Double(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("start_day", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'paused', 'closed')", name="ck_subscriptions_status"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_subscriptions_user_id"), "subscriptions", ["user_id"], unique=False)
    op.create_index(
        "uq_subscriptions_one_open_per_user",
        "subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.create_table(
        "books",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("processed_through", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind in ('system', 'manual')", name="ck_books_kind"),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "kind", name="uq_books_subscription_kind"),
    )
    op.create_index(op.f("ix_books_user_id"), "books", ["user_id"], unique=False)
    op.create_table(
        "param_changes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("old", postgresql.JSONB(), nullable=False),
        sa.Column("new", postgresql.JSONB(), nullable=False),
        sa.Column("effective_day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_param_changes_subscription_id"), "param_changes", ["subscription_id"], unique=False
    )
    op.create_index(op.f("ix_param_changes_user_id"), "param_changes", ["user_id"], unique=False)
    op.create_table(
        "book_days",
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("equity", sa.Double(), nullable=False),
        sa.Column("cash", sa.Double(), nullable=False),
        sa.Column("unit_nav", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("book_id", "day"),
    )
    op.create_index(op.f("ix_book_days_user_id"), "book_days", ["user_id"], unique=False)
    op.create_table(
        "followups",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("event_day", sa.Date(), nullable=False),
        sa.Column("base_adj_close", sa.Double(), nullable=False),
        sa.Column("ret_20", sa.Double(), nullable=True),
        sa.Column("ret_60", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("event in ('exit', 'abandoned', 'rejected')", name="ck_followups_event"),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "event", "symbol", "event_day", name="uq_followups_key"),
    )
    op.create_index(op.f("ix_followups_book_id"), "followups", ["book_id"], unique=False)
    op.create_index(op.f("ix_followups_user_id"), "followups", ["user_id"], unique=False)
    op.create_table(
        "rejections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rejections_book_id"), "rejections", ["book_id"], unique=False)
    op.create_index(op.f("ix_rejections_user_id"), "rejections", ["user_id"], unique=False)
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("drawdown", sa.Double(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id", "day", "kind", name="uq_risk_events_key"),
    )
    op.create_index(op.f("ix_risk_events_book_id"), "risk_events", ["book_id"], unique=False)
    op.create_index(op.f("ix_risk_events_user_id"), "risk_events", ["user_id"], unique=False)
    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("signal_day", sa.Date(), nullable=False),
        sa.Column("shares", sa.Double(), nullable=False),
        sa.Column("ref_price", sa.Double(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("stop_price", sa.Double(), nullable=True),
        sa.Column("take_price", sa.Double(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="created", nullable=False),
        sa.Column("status_day", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("reminded_day", sa.Date(), nullable=True),
        sa.Column("void_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('created', 'notified', 'executed', 'abandoned', 'voided', 'unnotified')",
            name="ck_signals_status",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "book_id", "side", "symbol", "signal_day", name="uq_signals_natural_key"
        ),
    )
    op.create_index(op.f("ix_signals_book_id"), "signals", ["book_id"], unique=False)
    op.create_index(op.f("ix_signals_user_id"), "signals", ["user_id"], unique=False)
    op.create_table(
        "system_states",
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of_day", sa.Date(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("book_id"),
    )
    op.create_index(op.f("ix_system_states_user_id"), "system_states", ["user_id"], unique=False)
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("entry_day", sa.Date(), nullable=False),
        sa.Column("exit_day", sa.Date(), nullable=False),
        sa.Column("shares", sa.Double(), nullable=False),
        sa.Column("cost", sa.Double(), nullable=False),
        sa.Column("proceeds", sa.Double(), nullable=False),
        sa.Column("cash_received", sa.Double(), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=False),
        sa.Column("holding_days", sa.Integer(), nullable=False),
        sa.Column("entry_metrics", postgresql.JSONB(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("signal_ref_price", sa.Double(), nullable=True),
        sa.Column("fill_price", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trades_book_id"), "trades", ["book_id"], unique=False)
    op.create_index(op.f("ix_trades_user_id"), "trades", ["user_id"], unique=False)
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("book_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("trade_day", sa.Date(), nullable=False),
        sa.Column("effective_day", sa.Date(), nullable=False),
        sa.Column("shares", sa.Double(), nullable=False),
        sa.Column("price", sa.Double(), nullable=False),
        sa.Column("fees", sa.Double(), nullable=False),
        sa.Column("fees_estimated", sa.Boolean(), nullable=False),
        sa.Column("cost_price", sa.Double(), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=True),
        sa.Column("voids_entry_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('buy', 'sell', 'transfer_in', 'deposit', 'withdraw', 'void')",
            name="ck_ledger_entries_kind",
        ),
        sa.CheckConstraint("origin in ('signal', 'self')", name="ck_ledger_entries_origin"),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["voids_entry_id"],
            ["ledger_entries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ledger_entries_book_id"), "ledger_entries", ["book_id"], unique=False)
    op.create_index(op.f("ix_ledger_entries_user_id"), "ledger_entries", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ledger_entries_user_id"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_book_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index(op.f("ix_trades_user_id"), table_name="trades")
    op.drop_index(op.f("ix_trades_book_id"), table_name="trades")
    op.drop_table("trades")
    op.drop_index(op.f("ix_system_states_user_id"), table_name="system_states")
    op.drop_table("system_states")
    op.drop_index(op.f("ix_signals_user_id"), table_name="signals")
    op.drop_index(op.f("ix_signals_book_id"), table_name="signals")
    op.drop_table("signals")
    op.drop_index(op.f("ix_risk_events_user_id"), table_name="risk_events")
    op.drop_index(op.f("ix_risk_events_book_id"), table_name="risk_events")
    op.drop_table("risk_events")
    op.drop_index(op.f("ix_rejections_user_id"), table_name="rejections")
    op.drop_index(op.f("ix_rejections_book_id"), table_name="rejections")
    op.drop_table("rejections")
    op.drop_index(op.f("ix_followups_user_id"), table_name="followups")
    op.drop_index(op.f("ix_followups_book_id"), table_name="followups")
    op.drop_table("followups")
    op.drop_index(op.f("ix_book_days_user_id"), table_name="book_days")
    op.drop_table("book_days")
    op.drop_index(op.f("ix_param_changes_user_id"), table_name="param_changes")
    op.drop_index(op.f("ix_param_changes_subscription_id"), table_name="param_changes")
    op.drop_table("param_changes")
    op.drop_index(op.f("ix_books_user_id"), table_name="books")
    op.drop_table("books")
    op.drop_index(
        "uq_subscriptions_one_open_per_user",
        table_name="subscriptions",
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.drop_index(op.f("ix_subscriptions_user_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index(op.f("ix_outbox_user_id"), table_name="outbox")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_audit_log_actor_user_id"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("users")
    op.drop_table("daily_runs")
