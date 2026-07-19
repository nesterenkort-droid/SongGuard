"""M3: subscriptions, notification_outbox

Revision ID: 0004_m3_bot
Revises: 0003_m2_detection
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_m3_bot"
down_revision = "0003_m2_detection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id", ondelete="CASCADE"), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="instant"),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "artist_id", name="uq_subscription_user_artist"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="finding"),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
    )
    op.create_index("ix_notification_outbox_user_id", "notification_outbox", ["user_id"])
    op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])
    op.create_index("ix_notification_outbox_scheduled_for", "notification_outbox", ["scheduled_for"])


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("subscriptions")
