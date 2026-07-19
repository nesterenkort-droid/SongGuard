"""M1: users, invites, artists, members, tracks, audit

Revision ID: 0002_m1_catalog
Revises: 0001_initial
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_m1_catalog"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("legal_address", sa.String(512), nullable=True),
        sa.Column("legal_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tg_user_id", name="uq_users_tg_user_id"),
    )
    op.create_index("ix_users_tg_user_id", "users", ["tg_user_id"])

    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("used_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token", name="uq_invites_token"),
    )
    op.create_index("ix_invites_token", "invites", ["token"])

    op.create_table(
        "artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("spotify_artist_id", sa.String(64), nullable=True),
        sa.Column("apple_artist_id", sa.String(64), nullable=True),
        sa.Column("yt_topic_channel_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_artists_name", "artists", ["name"])

    op.create_table(
        "artist_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("artist_id", "user_id", name="uq_artist_member"),
    )

    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("primary_artist_id", sa.Integer(), sa.ForeignKey("artists.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("normalized_title", sa.String(512), nullable=False),
        sa.Column("credit", sa.String(512), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("isrc", sa.String(32), nullable=True),
        sa.Column("upc", sa.String(32), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("spotify_track_id", sa.String(64), nullable=True),
        sa.Column("spotify_album_id", sa.String(64), nullable=True),
        sa.Column("apple_track_id", sa.BigInteger(), nullable=True),
        sa.Column("apple_collection_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("is_variant", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("variant_label", sa.String(64), nullable=True),
        sa.Column("original_audio_path", sa.String(512), nullable=True),
        sa.Column("preview_url", sa.String(1024), nullable=True),
        sa.Column("audio_ref_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("cover_path", sa.String(512), nullable=True),
        sa.Column("cover_url", sa.String(1024), nullable=True),
        sa.Column("cover_phash", sa.String(32), nullable=True),
        sa.Column("cover_dhash", sa.String(32), nullable=True),
        sa.Column("is_hot_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_scanned_youtube", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scanned_spotify", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scanned_apple", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("apple_track_id", name="uq_track_apple_id"),
        sa.UniqueConstraint("spotify_track_id", name="uq_track_spotify_id"),
    )
    op.create_index("ix_tracks_primary_artist_id", "tracks", ["primary_artist_id"])
    op.create_index("ix_tracks_normalized_title", "tracks", ["normalized_title"])
    op.create_index("ix_tracks_isrc", "tracks", ["isrc"])

    op.create_table(
        "track_artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("track_id", "artist_id", name="uq_track_artist"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("track_artists")
    op.drop_table("tracks")
    op.drop_table("artist_members")
    op.drop_table("artists")
    op.drop_table("invites")
    op.drop_table("users")
