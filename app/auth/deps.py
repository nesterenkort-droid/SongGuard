"""Auth dependencies for FastAPI routes.

`current_user` reads the signed session cookie and loads the user. `require_user`
and `require_admin` raise redirect/forbidden exceptions handled in app.main.
"""

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User


class NotAuthenticated(Exception):
    """Raised when a page requires login and there is no session."""


class NotAuthorized(Exception):
    """Raised when a logged-in user lacks the required role."""


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return await session.get(User, user_id)


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise NotAuthorized()
    return user
