"""Enforce one active store per Telegram chat

Revision ID: 012
Revises: 011
Create Date: 2026-04-30
"""

from alembic import op


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_stores_active_chat_id
        ON platform.stores (chat_id)
        WHERE is_active = true
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.uq_platform_stores_active_chat_id")
