"""Passwordless Telegram deep-link auth.

Flow:
  1. Browser asks to log in → we mint a short-lived *nonce* in Redis and show the
     user a link `t.me/<bot>?start=login-<nonce>` (or `join-<nonce>` for invites).
  2. The user opens Telegram; the bot receives `/start <payload>` and calls
     `confirm_start`, which identifies/creates the user and marks the nonce
     authenticated.
  3. The browser polls; when the nonce flips to authenticated it receives a session.

`confirm_start` holds all the real login/registration/authorization logic and is
shared by the bot and (in development only) a test endpoint, so the exact same code
path is exercised whether or not a live bot token is configured.
"""

import json
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Invite, User
from app.redis_client import redis_client
from app.services import audit

NONCE_PREFIX = "login:nonce:"
MODE_LOGIN = "login"
MODE_JOIN = "join"


async def create_nonce(mode: str, invite_token: str | None = None) -> str:
    nonce = secrets.token_urlsafe(24)
    payload = {"status": "pending", "mode": mode, "invite_token": invite_token, "user_id": None}
    await redis_client.set(
        NONCE_PREFIX + nonce, json.dumps(payload), ex=settings.login_nonce_ttl_seconds
    )
    return nonce


async def get_nonce(nonce: str) -> dict | None:
    raw = await redis_client.get(NONCE_PREFIX + nonce)
    return json.loads(raw) if raw else None


async def _save_nonce(nonce: str, payload: dict) -> None:
    await redis_client.set(
        NONCE_PREFIX + nonce, json.dumps(payload), ex=settings.login_nonce_ttl_seconds
    )


def build_deeplink(mode: str, nonce: str) -> str | None:
    """Telegram deep-link the user clicks. None if the bot username isn't set yet."""
    if not settings.telegram_bot_username:
        return None
    return f"https://t.me/{settings.telegram_bot_username}?start={mode}-{nonce}"


async def confirm_start(
    session: AsyncSession, payload: str, tg_user_id: int, display_name: str
) -> dict:
    """Called when a user opens the deep-link in Telegram (or the dev endpoint).

    Returns {ok, message, registered?, user_id?}. On success the referenced nonce is
    flipped to authenticated so the waiting browser can pick up a session.
    """
    mode, _, nonce = payload.partition("-")
    if not nonce:
        return {"ok": False, "message": "Некорректная ссылка входа."}

    state = await get_nonce(nonce)
    if state is None:
        return {
            "ok": False,
            "message": "Ссылка входа устарела — обновите страницу и попробуйте снова.",
        }

    now = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.tg_user_id == tg_user_id))
    is_bootstrap_admin = tg_user_id in settings.admin_ids
    registered = False

    if user is None:
        if is_bootstrap_admin:
            user = User(tg_user_id=tg_user_id, display_name=display_name, is_admin=True)
            session.add(user)
            registered = True
        else:
            invite = None
            invite_token = state.get("invite_token")
            if invite_token:
                invite = await session.scalar(
                    select(Invite).where(Invite.token == invite_token)
                )
            if invite is None or not invite.is_open(now):
                return {
                    "ok": False,
                    "message": "Регистрация возможна только по действующему приглашению.",
                }
            user = User(tg_user_id=tg_user_id, display_name=display_name, is_admin=False)
            session.add(user)
            await session.flush()
            invite.used_by_user_id = user.id
            invite.used_at = now
            registered = True
    else:
        if is_bootstrap_admin and not user.is_admin:
            user.is_admin = True
        if display_name:
            user.display_name = display_name

    await session.flush()
    await audit.log(
        session,
        actor_user_id=user.id,
        action="user.register" if registered else "user.login",
        entity_type="user",
        entity_id=user.id,
        summary=("Регистрация" if registered else "Вход")
        + f" через Telegram: {display_name} (tg {tg_user_id})",
    )

    state["status"] = "authenticated"
    state["user_id"] = user.id
    await _save_nonce(nonce, state)
    await session.commit()

    return {
        "ok": True,
        "registered": registered,
        "user_id": user.id,
        "message": "Готово! Вернитесь на сайт — вход выполнен.",
    }
