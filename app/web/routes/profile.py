"""User profile: legal identity for DMCA-style complaints (PLAN.md §11).

Packet generation is blocked without a real name/address/email — this is the only
place those get filled in.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.db import get_session
from app.models import User
from app.services import audit
from app.web.templating import render

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("")
async def profile_page(request: Request, user: User = Depends(require_user)):
    return render(request, "profile.html", user=user)


@router.post("")
async def profile_save(
    request: Request,
    legal_name: str = Form(""),
    legal_address: str = Form(""),
    legal_email: str = Form(""),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    user.legal_name = legal_name.strip() or None
    user.legal_address = legal_address.strip() or None
    user.legal_email = legal_email.strip() or None
    await audit.log(
        session,
        actor_user_id=user.id,
        action="user.update_legal",
        entity_type="user",
        entity_id=user.id,
        summary="Обновлены юридические данные для пакетов жалоб",
    )
    await session.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)
