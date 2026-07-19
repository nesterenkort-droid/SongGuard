"""Authentication routes: Telegram deep-link login + invites."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.deps import current_user
from app.config import settings
from app.db import SessionLocal, get_session
from app.models import Invite, User
from app.web.templating import render

router = APIRouter(tags=["auth"])


@router.get("/login")
async def login_page(request: Request, user: User | None = Depends(current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", user=None)


@router.post("/login/start")
async def login_start(request: Request):
    nonce = await auth_service.create_nonce(auth_service.MODE_LOGIN)
    deeplink = auth_service.build_deeplink(auth_service.MODE_LOGIN, nonce)
    return render(
        request,
        "_login_wait.html",
        {"nonce": nonce, "mode": auth_service.MODE_LOGIN, "deeplink": deeplink},
    )


@router.get("/join/{token}")
async def join_page(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
):
    invite = await session.scalar(select(Invite).where(Invite.token == token))
    valid = invite is not None and invite.is_open(datetime.now(UTC))
    return render(request, "join.html", {"token": token, "valid": valid})


@router.post("/join/{token}/start")
async def join_start(request: Request, token: str):
    nonce = await auth_service.create_nonce(auth_service.MODE_JOIN, invite_token=token)
    deeplink = auth_service.build_deeplink(auth_service.MODE_JOIN, nonce)
    return render(
        request,
        "_login_wait.html",
        {"nonce": nonce, "mode": auth_service.MODE_JOIN, "deeplink": deeplink},
    )


@router.get("/login/poll")
async def login_poll(
    request: Request, nonce: str, session: AsyncSession = Depends(get_session)
):
    state = await auth_service.get_nonce(nonce)
    if state is None:
        return render(request, "_login_expired.html")
    if state.get("status") == "authenticated" and state.get("user_id"):
        request.session["user_id"] = state["user_id"]
        await auth_service.redis_client.delete(auth_service.NONCE_PREFIX + nonce)
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp
    return render(request, "_login_pending.html", {"nonce": nonce})


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/dev/tg-confirm")
async def dev_tg_confirm(
    payload: str = Form(...),
    tg_user_id: int = Form(...),
    display_name: str = Form("Dev User"),
):
    """Development-only: simulate the bot confirming a deep-link.

    Runs the exact `confirm_start` logic the real bot runs. Disabled in production.
    """
    if settings.is_production:
        return JSONResponse({"error": "not found"}, status_code=404)
    async with SessionLocal() as session:
        result = await auth_service.confirm_start(session, payload, tg_user_id, display_name)
    code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=code)
