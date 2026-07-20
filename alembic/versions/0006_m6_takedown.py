"""M6: evidence_archive, takedown_packets

Revision ID: 0006_m6_takedown
Revises: 44d4d95703c6
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_m6_takedown"
down_revision = "44d4d95703c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_archive",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("cover_snapshot_path", sa.String(512), nullable=True),
        sa.Column("audio_match_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("llm_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("finding_id", name="uq_evidence_archive_finding"),
    )
    op.create_index("ix_evidence_archive_finding_id", "evidence_archive", ["finding_id"])

    op.create_table(
        "takedown_packets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence_archive.id"), nullable=False),
        sa.Column("route", sa.String(24), nullable=False),
        sa.Column("body_en", sa.String(8000), nullable=False),
        sa.Column("note_ru", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("sent_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_takedown_packets_finding_id", "takedown_packets", ["finding_id"])
    op.create_index("ix_takedown_packets_route", "takedown_packets", ["route"])
    op.create_index("ix_takedown_packets_status", "takedown_packets", ["status"])


def downgrade() -> None:
    op.drop_table("takedown_packets")
    op.drop_table("evidence_archive")
