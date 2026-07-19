"""Admin routes: invites, users, recent audit."""

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_admin
from app.config import settings
from app.db import get_session
from app.models import AuditEvent, Invite, User
from app.services import audit
from app.web.templating import render

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("")
async def admin_index(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    invites = list(await session.scalars(select(Invite).order_by(desc(Invite.created_at))))
    users = list(await session.scalars(select(User).order_by(User.created_at)))
    events = list(
        await session.scalars(
            select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(25)
        )
    )

    now = datetime.now(UTC)
    invite_rows = []
    for inv in invites:
        if inv.used_by_user_id is not None:
            status = "использовано"
        elif inv.expires_at is not None and inv.expires_at < now:
            status = "истекло"
        else:
            status = "открыто"
        invite_rows.append(
            {"inv": inv, "status": status, "url": f"{settings.base_url}/join/{inv.token}"}
        )

    return render(
        request,
        "admin.html",
        {"invite_rows": invite_rows, "users": users, "events": events},
        user=user,
    )


@router.post("/invite")
async def create_invite(
    note: str = Form(""),
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    token = secrets.token_urlsafe(16)
    invite = Invite(token=token, created_by_user_id=user.id, note=note.strip() or None)
    session.add(invite)
    await session.flush()
    await audit.log(
        session,
        actor_user_id=user.id,
        action="invite.create",
        entity_type="invite",
        entity_id=invite.id,
        summary="Создано приглашение" + (f" для «{note.strip()}»" if note.strip() else ""),
    )
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/invite/{invite_id}/revoke")
async def revoke_invite(
    invite_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    invite = await session.get(Invite, invite_id)
    if invite is not None and invite.used_by_user_id is None:
        invite.expires_at = datetime.now(UTC)
        await audit.log(
            session,
            actor_user_id=user.id,
            action="invite.revoke",
            entity_type="invite",
            entity_id=invite.id,
            summary="Отозвано приглашение",
        )
        await session.commit()
    return RedirectResponse("/admin", status_code=303)
