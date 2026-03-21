"""Phase 2 — vendor_prices and store_health_scores tables

Revision ID: 002
Revises: 001
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_prices",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("invoice_date", sa.Date, nullable=False),
        sa.Column("invoice_id", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_vendor_prices_store_id", "vendor_prices", ["store_id"])
    op.create_index("ix_vendor_prices_vendor", "vendor_prices", ["vendor"])

    op.create_table(
        "store_health_scores",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("over_short_avg", sa.Numeric(10, 2), server_default="0"),
        sa.Column("expense_ratio", sa.Numeric(5, 4), server_default="0"),
        sa.Column("invoice_match_rate", sa.Numeric(5, 4), server_default="0"),
        sa.Column("details", JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "week_start", name="uq_health_store_week"),
    )
    op.create_index("ix_store_health_scores_store_id", "store_health_scores", ["store_id"])


def downgrade() -> None:
    op.drop_table("vendor_prices")
    op.drop_table("store_health_scores")
