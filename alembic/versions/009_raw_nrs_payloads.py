"""Phase 3: raw_nrs.raw_sales_payloads — stores every NRS API response before transform.

Revision ID: 009
Revises: 008
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_sales_payloads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("fetch_date", sa.Date(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="raw_nrs",
    )
    # Index for the two query patterns:
    # 1. "show me raw payload for moraine on 2026-04-15" → (store_id, fetch_date)
    # 2. Retention cleanup: delete rows where fetched_at < cutoff → (fetched_at)
    op.create_index("ix_raw_nrs_payloads_store_date",
                    "raw_sales_payloads", ["store_id", "fetch_date"], schema="raw_nrs")
    op.create_index("ix_raw_nrs_payloads_fetched_at",
                    "raw_sales_payloads", ["fetched_at"], schema="raw_nrs")


def downgrade() -> None:
    op.drop_index("ix_raw_nrs_payloads_fetched_at", table_name="raw_sales_payloads", schema="raw_nrs")
    op.drop_index("ix_raw_nrs_payloads_store_date", table_name="raw_sales_payloads", schema="raw_nrs")
    op.drop_table("raw_sales_payloads", schema="raw_nrs")
