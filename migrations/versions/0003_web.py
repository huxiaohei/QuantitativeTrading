"""网页与登录：邀请码、会话、邮件令牌、限流事件、重跑请求，及 users / audit_log 新列。

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("abandon_mail", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "audit_log",
        sa.Column("channel", sa.String(length=8), server_default="cli", nullable=False),
    )
    op.create_check_constraint(
        "ck_audit_log_channel", "audit_log", "channel in ('web', 'cli', 'job')"
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=200), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_web_sessions_user_id"), "web_sessions", ["user_id"], unique=False)
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('verify', 'reset')", name="ck_email_tokens_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_email_tokens_user_id"), "email_tokens", ["user_id"], unique=False)
    op.create_table(
        "rate_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rate_events_key_at", "rate_events", ["key", "at"], unique=False)
    op.create_table(
        "rerun_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("rerun_requests")
    op.drop_index("ix_rate_events_key_at", table_name="rate_events")
    op.drop_table("rate_events")
    op.drop_index(op.f("ix_email_tokens_user_id"), table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_index(op.f("ix_web_sessions_user_id"), table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_table("invites")
    op.drop_constraint("ck_audit_log_channel", "audit_log", type_="check")
    op.drop_column("audit_log", "channel")
    op.drop_column("users", "abandon_mail")
