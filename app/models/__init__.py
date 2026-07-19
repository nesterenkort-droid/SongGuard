"""ORM models.

Importing everything here means `app.models.Base.metadata` is complete, which is
what Alembic's autogenerate and the metadata target rely on.
"""

from app.models.artist import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER, Artist, ArtistMember
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.system import SystemInfo
from app.models.track import (
    AUDIO_REF_FULL,
    AUDIO_REF_NONE,
    AUDIO_REF_PREVIEW,
    Track,
    TrackArtist,
)
from app.models.user import Invite, User

__all__ = [
    "Base",
    "SystemInfo",
    "User",
    "Invite",
    "Artist",
    "ArtistMember",
    "Track",
    "TrackArtist",
    "AuditEvent",
    "ROLE_OWNER",
    "ROLE_EDITOR",
    "ROLE_VIEWER",
    "AUDIO_REF_NONE",
    "AUDIO_REF_PREVIEW",
    "AUDIO_REF_FULL",
]
