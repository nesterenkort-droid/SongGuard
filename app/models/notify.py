"""Notifications: per-user subscriptions and the delivery outbox.

`Subscription` says *how* a user wants to hear about an artist's findings (instant /
daily / weekly digest, optional quiet hours). `NotificationOutbox` is the durable
queue the bot drains: every notification is a row with a `dedupe_key`, so a bot
restart mid-send neither loses nor repeats it (PLAN.md §9).
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

MODE_INSTANT = "instant"
MODE_DAILY = "daily"
MODE_WEEKLY = "weekly"

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"

KIND_FINDING = "finding"
KIND_DIGEST = "digest"
KIND_ADMIN_ALERT = "admin_alert"

MAX_OUTBOX_ATTEMPTS = 5


class Subscription(Base):
    """How a user wants to hear about one artist's findings (or all, if artist_id is
    NULL). Members with no row here get the implicit default: instant, no quiet hours
    (services/notify.py)."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "artist_id", name="uq_subscription_user_artist"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(16), default=MODE_INSTANT)
    quiet_hours_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-23 UTC
    quiet_hours_end: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-23 UTC
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NotificationOutbox(Base):
    """One durable, dedupe-keyed notification to deliver to one user via the bot."""

    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default=KIND_FINDING)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    # finding_id is set for kind=finding; digests/alerts carry their content in `data`.
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), nullable=True
    )
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=OUTBOX_PENDING, index=True)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
