"""Artists and per-user membership.

An Artist is a monitored catalog owner (one per imported platform artist). Membership
links users to artists with a role, so colleagues only see/manage their own artists
while collaborations can be shared.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)

    spotify_artist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    apple_artist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    yt_topic_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    members: Mapped[list["ArtistMember"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )
    tracks: Mapped[list["Track"]] = relationship(  # noqa: F821
        back_populates="primary_artist"
    )


class ArtistMember(Base):
    __tablename__ = "artist_members"
    __table_args__ = (UniqueConstraint("artist_id", "user_id", name="uq_artist_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_OWNER)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    artist: Mapped["Artist"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")  # noqa: F821
