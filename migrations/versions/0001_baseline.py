"""基线：空迁移，仅建立版本记录。

Revision ID: 0001
Revises:
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """基线没有任何表。"""


def downgrade() -> None:
    """基线没有任何表。"""
