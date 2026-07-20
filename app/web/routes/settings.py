"""Global settings: our own labels/distributors + allowed YouTube channels.

Everything here is a `WhitelistEntry` with scope=global (artist_id=NULL) — already
picked up automatically by `detection.build_context` for every artist, no per-artist
duplication needed. Admin-only: these rules affect detection for the whole catalog.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_admin
from app.db import get_session
from app.models import (
    WL_CHANNEL,
    WL_OWN_LABEL,
    WL_SCOPE_GLOBAL,
    User,
    WhitelistEntry,
)
from app.services import audit
from app.services.scoring import normalize_label
from app.web.templating import render

router = APIRouter(prefix="/settings", tags=["settings"])

ENTRY_LABELS = {
    WL_OWN_LABEL: "наш лейбл/дистрибьютор",
    WL_CHANNEL: "разрешённый канал",
}


@router.get("")
async def settings_page(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = list(
        await session.scalars(
            select(WhitelistEntry)
            .where(WhitelistEntry.scope == WL_SCOPE_GLOBAL)
            .order_by(WhitelistEntry.entry_type, WhitelistEntry.created_at.desc())
        )
    )
    own_labels = [r for r in rows if r.entry_type == WL_OWN_LABEL]
    channels = [r for r in rows if r.entry_type == WL_CHANNEL]
    return render(
        request,
        "settings.html",
        {"own_labels": own_labels, "channels": channels},
        user=user,
    )


@router.post("/whitelist")
async def add_whitelist(
    entry_type: str = Form(...),
    value: str = Form(...),
    note: str = Form(""),
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if entry_type not in (WL_OWN_LABEL, WL_CHANNEL):
        return RedirectResponse("/settings", status_code=303)
    value = value.strip()
    if not value:
        return RedirectResponse("/settings", status_code=303)

    entry = WhitelistEntry(
        scope=WL_SCOPE_GLOBAL,
        artist_id=None,
        entry_type=entry_type,
        value=value,
        normalized_value=normalize_label(value),
        note=note.strip() or None,
        created_by_user_id=user.id,
    )
    session.add(entry)
    await audit.log(
        session,
        actor_user_id=user.id,
        action="whitelist.add",
        entity_type="whitelist_entry",
        summary=f"Добавлено в белый список ({ENTRY_LABELS.get(entry_type, entry_type)}): «{value}»",
        data={"entry_type": entry_type, "value": value},
    )
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/whitelist/{entry_id}/delete")
async def delete_whitelist(
    entry_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    entry = await session.get(WhitelistEntry, entry_id)
    if entry is not None and entry.scope == WL_SCOPE_GLOBAL:
        await audit.log(
            session,
            actor_user_id=user.id,
            action="whitelist.delete",
            entity_type="whitelist_entry",
            entity_id=entry.id,
            summary=f"Удалено из белого списка: «{entry.value}»",
        )
        await session.delete(entry)
        await session.commit()
    return RedirectResponse("/settings", status_code=303)
