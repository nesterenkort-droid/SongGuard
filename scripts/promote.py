"""Promote a Telegram user to admin (recovery / bootstrap helper).

Usage:  python -m scripts.promote <tg_user_id> [display_name]

Creates the user if they don't exist yet. Use this if you lose admin access or to
grant admin without going through ADMIN_TG_IDS.
"""

import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.services import audit


async def main(tg_user_id: int, display_name: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_user_id == tg_user_id))
        if user is None:
            user = User(tg_user_id=tg_user_id, display_name=display_name, is_admin=True)
            session.add(user)
            action, msg = "user.register", f"Создан админ tg {tg_user_id}"
        else:
            user.is_admin = True
            action, msg = "user.promote", f"Назначен админом tg {tg_user_id}"
        await session.flush()
        await audit.log(
            session, actor_user_id=user.id, action=action, entity_type="user",
            entity_id=user.id, summary=msg,
        )
        await session.commit()
        print(f"OK: {msg} (user id {user.id})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.promote <tg_user_id> [display_name]")
        raise SystemExit(1)
    _tg_id = int(sys.argv[1])
    _name = sys.argv[2] if len(sys.argv) > 2 else f"admin-{_tg_id}"
    asyncio.run(main(_tg_id, _name))
