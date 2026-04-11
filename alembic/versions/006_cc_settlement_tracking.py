"""Add cc_settled_at / cc_bank_txn_id to daily_sales for CC settlement tracking.

Revision ID: 006
Revises: 005
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_sales",
        sa.Column("cc_settled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "daily_sales",
        sa.Column("cc_bank_txn_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_sales", "cc_bank_txn_id")
    op.drop_column("daily_sales", "cc_settled_at")
