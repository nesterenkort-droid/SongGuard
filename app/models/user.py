"""Users and invites.

Auth is passwordless: identity is a Telegram user id. Registration is invite-only
except for admins bootstrapped via ADMIN_TG_IDS. Legal fields are collected here
because DMCA takedown packets (M6) require a real name/address under penalty of
perjury.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Legal identity for DMCA (filled in later; blocks packet generation until set).
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    legal_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    legal_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    memberships: Mapped[list["ArtistMember"]] = relationship(  # noqa: F821
        back_populates="user"
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    used_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def is_open(self, now: datetime) -> bool:
        """True if this invite can still be used."""
        if self.used_by_user_id is not None:
            return False
        if self.expires_at is not None and self.expires_at < now:
            return False
        return True
