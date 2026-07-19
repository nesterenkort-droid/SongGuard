"""Detection core: candidates, findings, whitelist, pirate entities.

The key data-model principle (PLAN.md §6): **candidates are global, findings are
per-track**. Colleagues have overlapping catalogs (collabs), so one pirate release
must not be stored, pinged, or DMCA'd twice. A `PlatformCandidate` is therefore a
single global row per (platform, native_id); a `Finding` links a candidate to one of
our tracks with an explainable score.

`signals` on a Finding is a JSONB list where every entry keeps its raw value,
normalized contribution, weight, and the thresholds version — without that we could
neither debug nor later tune the scorer (PLAN.md §6, §7 Ярус 2/3).
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# --- Candidate platforms ---
PLATFORM_SPOTIFY = "spotify"
PLATFORM_ITUNES = "itunes"
PLATFORM_YOUTUBE = "youtube"  # populated in M4

# --- Finding score bands (PLAN.md §7 Ярус 3) ---
BAND_HIGH = "high"  # >=70 → finding straight away
BAND_MID = "mid"  # 40..69 → audio/LLM check queue (M5)
BAND_LOW = "low"  # <40 → logged only

# --- Finding lifecycle (M2 subset of PLAN.md §6). Later milestones add the
# packet/removed/reappeared tail. `tolerated` != `dismissed`: it suppresses repeat
# flags for a fan channel we allow, but the candidate stays visible.
STATUS_DETECTED = "detected"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_REMIX_REVIEW = "remix_review"
STATUS_CONFIRMED = "confirmed"
STATUS_DISMISSED = "dismissed"
STATUS_TOLERATED = "tolerated"

# Statuses that mean "already decided, don't surface as new work".
RESOLVED_STATUSES = frozenset({STATUS_CONFIRMED, STATUS_DISMISSED, STATUS_TOLERATED})

# --- Whitelist entry types (PLAN.md §6). `own_label` declares a tenant's own
# distributor/label strings so anything outside the list reads as foreign. ---
WL_OWN_LABEL = "own_label"
WL_ISRC = "isrc"
WL_SPOTIFY_ARTIST = "spotify_artist_id"
WL_APPLE_ARTIST = "apple_artist_id"
WL_CHANNEL = "channel_id"
WL_PLATFORM_ID = "platform_id"

WL_SCOPE_GLOBAL = "global"
WL_SCOPE_ARTIST = "artist"
WL_SCOPE_TRACK = "track"

# --- Pirate entity types (watchlist + recidivism evidence, PLAN.md §6) ---
PE_YT_CHANNEL = "yt_channel"
PE_SPOTIFY_LABEL = "spotify_label"
PE_APPLE_LABEL = "apple_label"
PE_DISTRIBUTOR = "distributor"


class PlatformCandidate(Base):
    """A single global row per (platform, native_id) — a thing we saw that might be
    piracy. Enriched with everything the scanners can cheaply capture."""

    __tablename__ = "platform_candidates"
    __table_args__ = (
        UniqueConstraint("platform", "native_id", name="uq_candidate_platform_native"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    native_id: Mapped[str] = mapped_column(String(128))

    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    normalized_title: Mapped[str] = mapped_column(String(512), index=True)
    uploader: Mapped[str | None] = mapped_column(String(512), nullable=True)  # artist/channel
    description_raw: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    # Parsed provenance (from description / album copyrights).
    parsed_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parsed_plabel: Mapped[str | None] = mapped_column(String(255), nullable=True)

    isrc: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    thumb_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cover_phash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cover_dhash: Mapped[str | None] = mapped_column(String(32), nullable=True)

    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    findings: Mapped[list["Finding"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class Finding(Base):
    """A candidate scored against one of our tracks. UNIQUE(candidate, track)."""

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("candidate_id", "track_id", name="uq_finding_candidate_track"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("platform_candidates.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )

    score: Mapped[int] = mapped_column(Integer, default=0)
    band: Mapped[str] = mapped_column(String(8), default=BAND_LOW, index=True)
    status: Mapped[str] = mapped_column(String(24), default=STATUS_DETECTED, index=True)
    thresholds_version: Mapped[str] = mapped_column(String(32), default="")

    # Explainable breakdown: [{key, label, raw, contribution, weight}, ...].
    signals: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Reserved for later milestones; nullable so M2 leaves them empty.
    audio_match: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # M5
    llm: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # M5

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    candidate: Mapped["PlatformCandidate"] = relationship(back_populates="findings")
    track: Mapped["Track"] = relationship()  # noqa: F821
    events: Mapped[list["FindingEvent"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class FindingEvent(Base):
    """Full audit of a finding's lifecycle (who moved it, from/to, when)."""

    __tablename__ = "finding_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    finding: Mapped["Finding"] = relationship(back_populates="events")


class WhitelistEntry(Base):
    """A rule that marks something as ours / allowed. Scoped global, per-artist, or
    per-track. `own_label` entries are how a tenant declares its legal distributors."""

    __tablename__ = "whitelist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(8), default=WL_SCOPE_ARTIST, index=True)
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), nullable=True, index=True
    )
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(String(24), index=True)
    value: Mapped[str] = mapped_column(String(512))
    normalized_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PirateEntity(Base):
    """A known-bad channel / label / distributor. Watchlist + recidivism evidence."""

    __tablename__ = "pirate_entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "normalized_value", name="uq_pirate_entity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[str] = mapped_column(String(512))
    normalized_value: Mapped[str] = mapped_column(String(512), index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
