"""Initial schema — all tables

Revision ID: 001
Revises:
Create Date: 2026-03-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── pending_state ────────────────────────────────────────────────────────
    op.create_table(
        "pending_state",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("state_key", sa.String(64), nullable=False),
        sa.Column("state_data", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "state_key", name="uq_pending_store_key"),
    )
    op.create_index("ix_pending_state_store_id", "pending_state", ["store_id"])

    # ── daily_sales ──────────────────────────────────────────────────────────
    op.create_table(
        "daily_sales",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("sale_date", sa.Date, nullable=False),
        # Left side
        sa.Column("product_sales", sa.Numeric(10, 2), server_default="0"),
        sa.Column("lotto_in", sa.Numeric(10, 2), server_default="0"),
        sa.Column("lotto_online", sa.Numeric(10, 2), server_default="0"),
        sa.Column("sales_tax", sa.Numeric(10, 2), server_default="0"),
        sa.Column("gpi", sa.Numeric(10, 2), server_default="0"),
        sa.Column("grand_total", sa.Numeric(10, 2), server_default="0"),
        sa.Column("refunds", sa.Numeric(10, 2), server_default="0"),
        # Right side manual
        sa.Column("lotto_po", sa.Numeric(10, 2), nullable=True),
        sa.Column("lotto_cr", sa.Numeric(10, 2), nullable=True),
        sa.Column("food_stamp", sa.Numeric(10, 2), nullable=True),
        # Payments
        sa.Column("cash_drop", sa.Numeric(10, 2), server_default="0"),
        sa.Column("card", sa.Numeric(10, 2), server_default="0"),
        sa.Column("check_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("atm", sa.Numeric(10, 2), server_default="0"),
        sa.Column("pull_tab", sa.Numeric(10, 2), server_default="0"),
        sa.Column("coupon", sa.Numeric(10, 2), server_default="0"),
        sa.Column("loyalty", sa.Numeric(10, 2), server_default="0"),
        sa.Column("vendor_payout", sa.Numeric(10, 2), server_default="0"),
        # Calculated
        sa.Column("total_payments", sa.Numeric(10, 2), nullable=True),
        sa.Column("over_short", sa.Numeric(10, 2), nullable=True),
        # JSON + misc
        sa.Column("departments", JSONB, server_default="[]"),
        sa.Column("total_transactions", sa.Integer, server_default="0"),
        # Audit
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "sale_date", name="uq_daily_sales_store_date"),
    )
    op.create_index("ix_daily_sales_store_id", "daily_sales", ["store_id"])

    # ── invoices ─────────────────────────────────────────────────────────────
    op.create_table(
        "invoices",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("invoice_date", sa.Date, nullable=False),
        sa.Column("invoice_num", sa.String(64), nullable=True),
        sa.Column("line_items", JSONB, nullable=True),
        sa.Column("matched_bank_transaction_id", sa.Integer, nullable=True),
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_invoices_store_id", "invoices", ["store_id"])

    # ── expenses ─────────────────────────────────────────────────────────────
    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("expense_date", sa.Date, nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_expenses_store_id", "expenses", ["store_id"])

    # ── rebates ──────────────────────────────────────────────────────────────
    op.create_table(
        "rebates",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("rebate_date", sa.Date, nullable=False),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_rebates_store_id", "rebates", ["store_id"])

    # ── revenues ─────────────────────────────────────────────────────────────
    op.create_table(
        "revenues",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("revenue_date", sa.Date, nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_revenues_store_id", "revenues", ["store_id"])

    # ── bank_transactions ────────────────────────────────────────────────────
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("transaction_date", sa.Date, nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("description", sa.String(256), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("transaction_type", sa.String(32), nullable=True),
        sa.Column("plaid_transaction_id", sa.String(128), unique=True, nullable=True),
        sa.Column("matched_invoice_id", sa.Integer, nullable=True),
        sa.Column("is_matched", sa.Boolean, server_default="false"),
        sa.Column("last_updated_by", sa.String(16), server_default="bot"),
        sa.Column("last_updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_bank_transactions_store_id", "bank_transactions", ["store_id"])

    # ── conversation_history ─────────────────────────────────────────────────
    op.create_table(
        "conversation_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_history_store_id", "conversation_history", ["store_id"])


def downgrade() -> None:
    op.drop_table("conversation_history")
    op.drop_table("bank_transactions")
    op.drop_table("revenues")
    op.drop_table("rebates")
    op.drop_table("expenses")
    op.drop_table("invoices")
    op.drop_table("daily_sales")
    op.drop_table("pending_state")
