"""Shared Jinja2 templates + a render helper.

`render` injects `settings` and the current `user` into every template so the base
layout can show the nav (login/logout, catalog, admin) consistently.
"""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import User

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=_templates_dir)


def render(
    request: Request,
    name: str,
    context: dict | None = None,
    *,
    user: User | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    ctx: dict = {"settings": settings, "user": user}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
