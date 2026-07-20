"""Add tracks.is_muted — excluded from scanning and hidden from findings

Revision ID: 0009_track_muted
Revises: 0008_whitelist_channel_url
Create Date: 2026-07-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_track_muted"
down_revision = "0008_whitelist_channel_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("is_muted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tracks", "is_muted")
