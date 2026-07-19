"""M2: platform_candidates, findings, finding_events, whitelist_entries, pirate_entities

Revision ID: 0003_m2_detection
Revises: 0002_m1_catalog
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_m2_detection"
down_revision = "0002_m1_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("native_id", sa.String(128), nullable=False),
        sa.Column("url", sa.String(1024), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("normalized_title", sa.String(512), nullable=False),
        sa.Column("uploader", sa.String(512), nullable=True),
        sa.Column("description_raw", sa.String(4096), nullable=True),
        sa.Column("parsed_provider", sa.String(255), nullable=True),
        sa.Column("parsed_plabel", sa.String(255), nullable=True),
        sa.Column("isrc", sa.String(32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("thumb_url", sa.String(1024), nullable=True),
        sa.Column("cover_phash", sa.String(32), nullable=True),
        sa.Column("cover_dhash", sa.String(32), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("platform", "native_id", name="uq_candidate_platform_native"),
    )
    op.create_index("ix_platform_candidates_platform", "platform_candidates", ["platform"])
    op.create_index("ix_platform_candidates_normalized_title", "platform_candidates", ["normalized_title"])
    op.create_index("ix_platform_candidates_isrc", "platform_candidates", ["isrc"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("platform_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("band", sa.String(8), nullable=False, server_default="low"),
        sa.Column("status", sa.String(24), nullable=False, server_default="detected"),
        sa.Column("thresholds_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("signals", postgresql.JSONB(), nullable=True),
        sa.Column("audio_match", postgresql.JSONB(), nullable=True),
        sa.Column("llm", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("candidate_id", "track_id", name="uq_finding_candidate_track"),
    )
    op.create_index("ix_findings_candidate_id", "findings", ["candidate_id"])
    op.create_index("ix_findings_track_id", "findings", ["track_id"])
    op.create_index("ix_findings_band", "findings", ["band"])
    op.create_index("ix_findings_status", "findings", ["status"])
    op.create_index("ix_findings_created_at", "findings", ["created_at"])

    op.create_table(
        "finding_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=True),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_finding_events_finding_id", "finding_events", ["finding_id"])

    op.create_table(
        "whitelist_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(8), nullable=False, server_default="artist"),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id", ondelete="CASCADE"), nullable=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("entry_type", sa.String(24), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column("normalized_value", sa.String(512), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_whitelist_entries_scope", "whitelist_entries", ["scope"])
    op.create_index("ix_whitelist_entries_artist_id", "whitelist_entries", ["artist_id"])
    op.create_index("ix_whitelist_entries_entry_type", "whitelist_entries", ["entry_type"])

    op.create_table(
        "pirate_entities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column("normalized_value", sa.String(512), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("entity_type", "normalized_value", name="uq_pirate_entity"),
    )
    op.create_index("ix_pirate_entities_entity_type", "pirate_entities", ["entity_type"])
    op.create_index("ix_pirate_entities_normalized_value", "pirate_entities", ["normalized_value"])


def downgrade() -> None:
    op.drop_table("pirate_entities")
    op.drop_table("whitelist_entries")
    op.drop_table("finding_events")
    op.drop_table("findings")
    op.drop_table("platform_candidates")
