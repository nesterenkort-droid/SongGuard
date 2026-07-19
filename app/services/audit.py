"""Audit logging helper.

Call `log(...)` for every state-changing action. It only adds the row to the
session; the caller commits within its own transaction so the audit entry and the
change it describes land atomically.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


async def log(
    session: AsyncSession,
    *,
    action: str,
    summary: str,
    actor_user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    data: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            data=data,
        )
    )
