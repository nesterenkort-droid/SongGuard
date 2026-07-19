"""System metadata table.

A tiny key/value store that proves the migration + ORM stack works end-to-end
in M0. Real domain models (users, artists, tracks, candidates, findings, …)
arrive in M1+.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SystemInfo(Base):
    __tablename__ = "system_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
