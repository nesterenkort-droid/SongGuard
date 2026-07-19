"""Tests for health endpoints and top-level routing.

Hermetic: `gather_health` is monkeypatched so no live DB/Redis is needed.
"""

from app.health import Component


def _fake_gather(overall, components):
    async def _inner():
        return overall, components

    return _inner


async def test_healthz_ok(client, monkeypatch):
    components = [Component("database", "База данных", "ok", "ok", True)]
    monkeypatch.setattr(
        "app.web.routes.health.gather_health", _fake_gather("ok", components)
    )
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"][0]["name"] == "database"


async def test_healthz_down_returns_503(client, monkeypatch):
    components = [Component("database", "База данных", "down", "нет связи", True)]
    monkeypatch.setattr(
        "app.web.routes.health.gather_health", _fake_gather("down", components)
    )
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "down"


async def test_index_requires_login(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_login_page_renders(client):
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "Вход в TrackGuard" in resp.text


async def test_openapi_available(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "TrackGuard"
