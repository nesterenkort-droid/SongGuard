"""Tracks and their credited artists.

A Track is one release we protect. `normalized_title` is the title folded for
matching (NFKC + confusables + suffix-strip); it is populated at import and reused
by the M2 detection signals. Platform ids are unique-when-present so re-imports are
idempotent. Variant flags support the "official variants" exclusion (the artist's
own Slowed/Nightcore versions must never be flagged as piracy).
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

AUDIO_REF_NONE = "none"
AUDIO_REF_PREVIEW = "preview"
AUDIO_REF_FULL = "full"


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (
        UniqueConstraint("apple_track_id", name="uq_track_apple_id"),
        UniqueConstraint("spotify_track_id", name="uq_track_spotify_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    primary_artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True
    )

    title: Mapped[str] = mapped_column(String(512))
    normalized_title: Mapped[str] = mapped_column(String(512), index=True)
    credit: Mapped[str | None] = mapped_column(String(512), nullable=True)

    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    isrc: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    upc: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Platform identifiers (nullable-unique → many NULLs allowed, re-import dedupes).
    spotify_track_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spotify_album_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    apple_track_id: Mapped[str | None] = mapped_column(BigInteger, nullable=True)
    apple_collection_id: Mapped[str | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # itunes/spotify/manual

    # Variant handling (official Slowed/Sped/Nightcore versions of our own tracks).
    is_variant: Mapped[bool] = mapped_column(Boolean, default=False)
    variant_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Audio reference for fingerprinting (M5).
    original_audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preview_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    audio_ref_status: Mapped[str] = mapped_column(String(16), default=AUDIO_REF_NONE)

    # Cover art + perceptual hashes.
    cover_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cover_phash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cover_dhash: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Scan scheduling.
    is_hot_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Muted = user doesn't care about this track: never scanned, and its findings are
    # hidden from the dashboard (reversible — unmute brings them back).
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scanned_youtube: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scanned_spotify: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scanned_apple: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    primary_artist: Mapped["Artist"] = relationship(back_populates="tracks")  # noqa: F821


class TrackArtist(Base):
    """Many-to-many credit link (collab tracks shared across our artists)."""

    __tablename__ = "track_artists"
    __table_args__ = (
        UniqueConstraint("track_id", "artist_id", name="uq_track_artist"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
