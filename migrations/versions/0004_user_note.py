"""使用者备注：users.note（只有管理员能看到和修改）。

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("note", sa.String(length=200), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "note")
