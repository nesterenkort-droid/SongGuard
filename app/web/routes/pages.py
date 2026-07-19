"""Human-facing pages: the logged-in dashboard + the health fragment."""

from fastapi import APIRouter, Depends, Request

from app.auth.deps import require_user
from app.health import gather_health
from app.models import User
from app.web.templating import render

router = APIRouter(tags=["pages"])


@router.get("/")
async def index(request: Request, user: User = Depends(require_user)):
    overall, components = await gather_health()
    return render(
        request, "index.html", {"overall": overall, "components": components}, user=user
    )


@router.get("/health/fragment")
async def health_fragment(request: Request):
    """HTMX polls this to refresh the health tiles. Public (no user data)."""
    overall, components = await gather_health()
    return render(request, "_tiles.html", {"overall": overall, "components": components})
