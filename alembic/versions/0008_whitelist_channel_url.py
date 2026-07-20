"""Add whitelist_entries.channel_url (optional link for channel entries)

Revision ID: 0008_whitelist_channel_url
Revises: 0007_licensed_content
Create Date: 2026-07-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_whitelist_channel_url"
down_revision = "0007_licensed_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whitelist_entries",
        sa.Column("channel_url", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whitelist_entries", "channel_url")
