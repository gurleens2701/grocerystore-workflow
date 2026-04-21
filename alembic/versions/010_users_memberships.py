"""platform.users + platform.user_store_memberships — per-user dashboard auth.

Each owner gets their own login. Memberships control which stores they can see.

Revision ID: 010
Revises: 009
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="platform",
    )
    op.create_index(
        "ix_platform_users_username", "users", ["username"],
        unique=True, schema="platform",
    )

    op.create_table(
        "user_store_memberships",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("platform.users.id"), nullable=False),
        sa.Column("store_id", sa.String(64), sa.ForeignKey("platform.stores.store_id"), nullable=False),
        sa.Column("role", sa.String(32), server_default=sa.text("'owner'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        schema="platform",
    )
    op.create_unique_constraint(
        "uq_user_store_membership", "user_store_memberships",
        ["user_id", "store_id"], schema="platform",
    )


def downgrade() -> None:
    op.drop_table("user_store_memberships", schema="platform")
    op.drop_index("ix_platform_users_username", table_name="users", schema="platform")
    op.drop_table("users", schema="platform")
