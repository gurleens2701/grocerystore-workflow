"""Phase 2 — invoice_items table for item-level price tracking

Revision ID: 003
Revises: 002
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("invoice_id", sa.Integer, nullable=True),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column("item_name", sa.String(256), nullable=False),
        sa.Column("item_name_raw", sa.String(256), nullable=False),
        sa.Column("upc", sa.String(32), nullable=True),
        sa.Column("unit_price", sa.Numeric(10, 4), nullable=False),
        sa.Column("case_price", sa.Numeric(10, 4), nullable=True),
        sa.Column("case_qty", sa.Integer, nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("invoice_date", sa.Date, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_invoice_items_store_id", "invoice_items", ["store_id"])
    op.create_index("ix_invoice_items_vendor", "invoice_items", ["vendor"])
    op.create_index("ix_invoice_items_upc", "invoice_items", ["upc"])
    op.create_index("ix_invoice_items_invoice_date", "invoice_items", ["invoice_date"])
    op.create_index("ix_invoice_items_item_name", "invoice_items", ["item_name"])


def downgrade() -> None:
    op.drop_table("invoice_items")
