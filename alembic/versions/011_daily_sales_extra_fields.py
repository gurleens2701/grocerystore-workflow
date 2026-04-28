"""Add extra_fields JSONB to canonical.daily_sales for store-specific manual fields

Revision ID: 011
Revises: 010
Create Date: 2026-04-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_sales",
        sa.Column("extra_fields", JSONB, nullable=False, server_default="{}"),
        schema="canonical",
    )


def downgrade() -> None:
    op.drop_column("daily_sales", "extra_fields", schema="canonical")
