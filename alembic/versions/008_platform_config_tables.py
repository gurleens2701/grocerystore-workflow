"""Phase 2: store_daily_report_rules, store_sheet_mappings, store_tool_policies,
store_bank_rules — all seeded with Moraine's current config.
Migrate clean rows from transaction_rules into store_bank_rules.

Revision ID: 008
Revises: 007
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. store_daily_report_rules ──────────────────────────────────────────
    # One row per field on the daily sheet. source='api' = comes from NRS, 'manual' = owner enters it.
    op.create_table(
        "store_daily_report_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("section", sa.String(8), nullable=False),      # left | right
        sa.Column("field_name", sa.String(64), nullable=False),  # canonical field key
        sa.Column("label", sa.String(64), nullable=False),        # display label (e.g. "IN. LOTTO")
        sa.Column("source", sa.String(8), nullable=False),        # api | manual
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("store_id", "field_name"),
        schema="platform",
    )

    # ── 2. store_sheet_mappings ──────────────────────────────────────────────
    # Maps field_name → column_index in Google Sheet for each section.
    # column_index is 1-based (matches gspread convention).
    op.create_table(
        "store_sheet_mappings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("section", sa.String(32), nullable=False),      # daily_sales | expenses | cogs | payroll
        sa.Column("field_name", sa.String(64), nullable=False),   # canonical key or dept.key
        sa.Column("column_index", sa.Integer(), nullable=False),  # 1-based
        sa.Column("column_header", sa.String(64), nullable=False),
        sa.UniqueConstraint("store_id", "section", "field_name"),
        schema="platform",
    )

    # ── 3. store_tool_policies ───────────────────────────────────────────────
    # Which agent tools are available for each store.
    op.create_table(
        "store_tool_policies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("store_id", "tool_name"),
        schema="platform",
    )

    # ── 4. store_bank_rules ──────────────────────────────────────────────────
    # Auto-categorization patterns for bank transactions. Replaces canonical.transaction_rules
    # as the read source; writes still go to both during transition.
    op.create_table(
        "store_bank_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("pattern", sa.String(256), nullable=False),
        sa.Column("reconcile_type", sa.String(32), nullable=False),
        sa.Column("reconcile_subcategory", sa.String(128), nullable=True),
        sa.Column("confirmed_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("store_id", "pattern"),
        schema="platform",
    )

    # ── Seed: Moraine daily report rules ─────────────────────────────────────
    # LEFT side — api-sourced (NRS fills these automatically)
    op.execute("""
        INSERT INTO platform.store_daily_report_rules
            (store_id, section, field_name, label, source, display_order)
        VALUES
            ('moraine', 'left', 'product_sales', 'TOTAL',     'api', 1),
            ('moraine', 'left', 'lotto_in',      'IN. LOTTO', 'api', 2),
            ('moraine', 'left', 'lotto_online',  'ON. LINE',  'api', 3),
            ('moraine', 'left', 'sales_tax',     'SALES TAX', 'api', 4),
            ('moraine', 'left', 'gpi',           'GPI',       'api', 5),
            ('moraine', 'right', 'lotto_po',     'LOTTO PO',  'manual', 1),
            ('moraine', 'right', 'lotto_cr',     'LOTTO CR',  'manual', 2),
            ('moraine', 'right', 'food_stamp',   'FOOD STAMP','manual', 3)
        ON CONFLICT (store_id, field_name) DO NOTHING
    """)

    # ── Seed: Moraine Google Sheet column mappings ────────────────────────────
    # Column indices are 1-based, derived from the positional order in log_daily_sales.
    op.execute("""
        INSERT INTO platform.store_sheet_mappings
            (store_id, section, field_name, column_index, column_header)
        VALUES
            ('moraine', 'daily_sales', 'date',          1,  'DATE'),
            ('moraine', 'daily_sales', 'dept.beer',     2,  'BEER'),
            ('moraine', 'daily_sales', 'dept.cigs',     3,  'CIGS'),
            ('moraine', 'daily_sales', 'dept.dairy',    4,  'DAIRY'),
            ('moraine', 'daily_sales', 'dept.n_tax',    5,  'N.TAX'),
            ('moraine', 'daily_sales', 'dept.tax',      6,  'TAX'),
            ('moraine', 'daily_sales', 'dept.ice',      7,  'ICE'),
            ('moraine', 'daily_sales', 'dept.lbait',    8,  'LBAIT'),
            ('moraine', 'daily_sales', 'dept.pizza',    9,  'PIZZA'),
            ('moraine', 'daily_sales', 'dept.pop',      10, 'POP'),
            ('moraine', 'daily_sales', 'dept.preroll',  11, 'PREROLL'),
            ('moraine', 'daily_sales', 'dept.tobbaco',  12, 'TOBBACO'),
            ('moraine', 'daily_sales', 'dept.vape',     13, 'VAPE'),
            ('moraine', 'daily_sales', 'dept.wine',     14, 'WINE'),
            ('moraine', 'daily_sales', 'dept.propane',  15, 'PROPANE'),
            ('moraine', 'daily_sales', 'product_sales', 16, 'SALE'),
            ('moraine', 'daily_sales', 'lotto_online',  17, 'ONLINE'),
            ('moraine', 'daily_sales', 'lotto_in',      18, 'INSTANT'),
            ('moraine', 'daily_sales', 'lotto_po',      19, 'LOTTO'),
            ('moraine', 'daily_sales', 'lotto_cr',      20, 'L.CREDIT'),
            ('moraine', 'daily_sales', 'atm',           21, 'ATM'),
            ('moraine', 'daily_sales', 'cash_drops',    22, 'CASH'),
            ('moraine', 'daily_sales', 'check',         23, 'CHECK'),
            ('moraine', 'daily_sales', 'card',          24, 'CREDIT'),
            ('moraine', 'daily_sales', 'coupon',        25, 'COUPON'),
            ('moraine', 'daily_sales', 'pull_tab',      26, 'P.TAB'),
            ('moraine', 'daily_sales', 'sales_tax',     27, 'S.TAX'),
            ('moraine', 'daily_sales', 'dept.payin',    28, 'PAYIN'),
            ('moraine', 'daily_sales', 'food_stamp',    29, 'FOODS'),
            ('moraine', 'daily_sales', 'vendor',        30, 'PAYOUT'),
            ('moraine', 'daily_sales', 'reason',        31, 'REASON'),
            ('moraine', 'daily_sales', 'loyalty',       32, '2 ALTRI'),
            ('moraine', 'daily_sales', 'grand_total',   33, 'G.TOT')
        ON CONFLICT (store_id, section, field_name) DO NOTHING
    """)

    # ── Seed: Moraine tool policies (all tools enabled) ───────────────────────
    op.execute("""
        INSERT INTO platform.store_tool_policies (store_id, tool_name, enabled)
        VALUES
            ('moraine', 'query_sales',             true),
            ('moraine', 'query_expenses',          true),
            ('moraine', 'query_invoices',          true),
            ('moraine', 'query_rebates',           true),
            ('moraine', 'query_revenue',           true),
            ('moraine', 'query_prices',            true),
            ('moraine', 'query_vendors',           true),
            ('moraine', 'query_ordered_items',     true),
            ('moraine', 'query_bank_transactions', true),
            ('moraine', 'log_expense',             true),
            ('moraine', 'log_invoice',             true),
            ('moraine', 'log_payroll',             true),
            ('moraine', 'log_rebate',              true),
            ('moraine', 'log_revenue',             true),
            ('moraine', 'log_daily_sales',         true),
            ('moraine', 'sync_sheets_now',         true)
        ON CONFLICT (store_id, tool_name) DO NOTHING
    """)

    # ── Seed: Moraine bank rules — migrate clean rows from transaction_rules ──
    # Skip rows where subcategory looks like a Telegram message (> 40 chars or has '?')
    op.execute("""
        INSERT INTO platform.store_bank_rules
            (store_id, pattern, reconcile_type, reconcile_subcategory, confirmed_count, last_seen_at)
        SELECT
            store_id,
            pattern,
            reconcile_type,
            CASE
                WHEN reconcile_subcategory IS NULL THEN NULL
                WHEN length(reconcile_subcategory) > 40 THEN NULL
                WHEN reconcile_subcategory LIKE '%?%' THEN NULL
                WHEN reconcile_subcategory LIKE '% %' AND length(reconcile_subcategory) > 20 THEN NULL
                ELSE reconcile_subcategory
            END,
            confirmed_count,
            last_seen_at
        FROM canonical.transaction_rules
        ON CONFLICT (store_id, pattern) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("store_bank_rules",         schema="platform")
    op.drop_table("store_tool_policies",      schema="platform")
    op.drop_table("store_sheet_mappings",     schema="platform")
    op.drop_table("store_daily_report_rules", schema="platform")
