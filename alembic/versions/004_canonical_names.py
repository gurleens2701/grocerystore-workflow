"""004 — Add canonical_name and confidence to invoice_items

Revision ID: 004
Revises: 003
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_items",
        sa.Column("canonical_name", sa.String(256), nullable=True),
    )
    op.add_column(
        "invoice_items",
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
    )
    op.create_index("ix_invoice_items_canonical_name", "invoice_items", ["canonical_name"])


def downgrade() -> None:
    op.drop_index("ix_invoice_items_canonical_name", table_name="invoice_items")
    op.drop_column("invoice_items", "confidence")
    op.drop_column("invoice_items", "canonical_name")
