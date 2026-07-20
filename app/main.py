"""FastAPI application entrypoint (the `web` service)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth.deps import NotAuthenticated, NotAuthorized
from app.config import settings
from app.web.routes import admin, auth, catalog, findings, health, pages, profile, quality
from app.web.templating import render

app = FastAPI(title="TrackGuard", version=settings.app_version)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.is_production,
)


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(NotAuthorized)
async def _not_authorized(request: Request, exc: NotAuthorized):
    return render(request, "403.html", status_code=403)


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(catalog.router)
app.include_router(findings.router)
app.include_router(profile.router)
app.include_router(quality.router)
app.include_router(admin.router)

# Static assets.
_static_dir = Path(__file__).parent / "web" / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Cover art is served publicly; original audio (settings.audio_dir) is NOT mounted.
Path(settings.cover_dir).mkdir(parents=True, exist_ok=True)
app.mount("/media/covers", StaticFiles(directory=settings.cover_dir), name="covers")
