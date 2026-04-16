"""Phase 1: create platform/canonical/ops schemas, platform config tables,
seed Moraine config, and move business data tables into canonical schema.

Revision ID: 007
Revises: 006
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


# Tables being moved from public → canonical schema
_CANONICAL_TABLES = [
    "daily_sales",
    "bank_transactions",
    "invoices",
    "expenses",
    "rebates",
    "revenues",
    "transaction_rules",
    "vendor_prices",
    "invoice_items",
    "store_health_scores",
    "message_log",
    "conversation_history",
]


def upgrade() -> None:
    # ── 1. Create schemas ────────────────────────────────────────────────────
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.execute("CREATE SCHEMA IF NOT EXISTS canonical")
    op.execute("CREATE SCHEMA IF NOT EXISTS raw_nrs")
    op.execute("CREATE SCHEMA IF NOT EXISTS ops")

    # ── 2. Platform config tables ────────────────────────────────────────────

    op.create_table(
        "stores",
        sa.Column("store_id", sa.String(64), primary_key=True),
        sa.Column("store_name", sa.String(128), nullable=False),
        sa.Column("pos_type", sa.String(32), nullable=False),        # nrs | modisoft | manual
        sa.Column("chat_id", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="platform",
    )

    op.create_table(
        "store_workflows",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False, unique=True),
        sa.Column("daily_report_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("daily_report_mode", sa.String(32), server_default="nrs_pull", nullable=False),
        sa.Column("manual_entry_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("nightly_sheet_sync", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("bank_recon_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("month_end_summary", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("weekly_bank_summary", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("invoice_ocr_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("unified_agent_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="platform",
    )

    op.create_table(
        "store_scheduler_policies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("job_name", sa.String(64), nullable=False),
        sa.Column("schedule", sa.String(64), nullable=False),         # cron expression or label
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("config", JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("store_id", "job_name"),
        schema="platform",
    )

    # ── 3. Ops tables ────────────────────────────────────────────────────────

    op.create_table(
        "job_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=True),           # nullable: platform-level jobs have no store
        sa.Column("job_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),            # started | success | failed
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ran_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="ops",
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),             # owner | bot | scheduler | admin
        sa.Column("action", sa.String(256), nullable=False),
        sa.Column("table_name", sa.String(64), nullable=True),
        sa.Column("record_id", sa.BigInteger(), nullable=True),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="ops",
    )

    # ── 4. Seed Moraine config ────────────────────────────────────────────────
    op.execute("""
        INSERT INTO platform.stores (store_id, store_name, pos_type, chat_id, timezone)
        VALUES ('moraine', 'Moraine Foodmart', 'nrs', '8525501774', 'America/New_York')
        ON CONFLICT (store_id) DO NOTHING
    """)

    op.execute("""
        INSERT INTO platform.store_workflows (
            store_id, daily_report_enabled, daily_report_mode,
            manual_entry_enabled, nightly_sheet_sync, bank_recon_enabled,
            month_end_summary, weekly_bank_summary, invoice_ocr_enabled, unified_agent_enabled
        ) VALUES (
            'moraine', true, 'nrs_pull',
            true, true, true,
            true, true, false, true
        )
        ON CONFLICT (store_id) DO NOTHING
    """)

    op.execute("""
        INSERT INTO platform.store_scheduler_policies (store_id, job_name, schedule, enabled) VALUES
            ('moraine', 'daily_fetch',     '0 7 * * *',     true),
            ('moraine', 'bank_sync',       'every_4h',       true),
            ('moraine', 'nightly_sync',    'every_15m',      true),
            ('moraine', 'weekly_summary',  '0 18 * * 0',    true),
            ('moraine', 'cashflow',        '0 8 L * *',      true)
        ON CONFLICT (store_id, job_name) DO NOTHING
    """)

    # ── 5. Move data tables to canonical schema ──────────────────────────────
    # ALTER TABLE SET SCHEMA is instant — no data is copied, just a metadata move.
    # Pre-migration row counts (recorded 2026-04-16):
    #   daily_sales=17, bank_transactions=649, invoices=25, expenses=10, rebates=6
    for table in _CANONICAL_TABLES:
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA canonical")


def downgrade() -> None:
    # Move tables back to public
    for table in _CANONICAL_TABLES:
        op.execute(f"ALTER TABLE canonical.{table} SET SCHEMA public")

    op.drop_table("audit_logs", schema="ops")
    op.drop_table("job_history", schema="ops")
    op.drop_table("store_scheduler_policies", schema="platform")
    op.drop_table("store_workflows", schema="platform")
    op.drop_table("stores", schema="platform")

    op.execute("DROP SCHEMA IF EXISTS ops")
    op.execute("DROP SCHEMA IF EXISTS raw_nrs")
    op.execute("DROP SCHEMA IF EXISTS canonical")
    op.execute("DROP SCHEMA IF EXISTS platform")
