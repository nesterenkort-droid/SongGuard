"""Add platform_candidates.licensed_content (weak YouTube provenance signal)

Revision ID: 0007_licensed_content
Revises: 0006_m6_takedown
Create Date: 2026-07-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_licensed_content"
down_revision = "0006_m6_takedown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_candidates",
        sa.Column("licensed_content", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_candidates", "licensed_content")
