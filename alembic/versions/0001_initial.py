"""initial: system_info

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-19

The first migration. Creates the tiny system_info table so the migration and ORM
stack is proven end-to-end in M0. Domain tables follow in M1+.
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_info",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("key", name="uq_system_info_key"),
    )
    op.create_index("ix_system_info_key", "system_info", ["key"])


def downgrade() -> None:
    op.drop_index("ix_system_info_key", table_name="system_info")
    op.drop_table("system_info")
