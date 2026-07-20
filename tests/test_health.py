"""Tests for health endpoints and top-level routing.

Hermetic: `gather_health` is monkeypatched so no live DB/Redis is needed.
"""

import pytest

from app import health
from app.health import Component
from app.redis_client import redis_client
from app.services.ops import YTDLP_DEGRADED_KEY


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


def test_check_disk_warns_at_80_percent(monkeypatch):
    class FakeUsage:
        total = 100
        used = 85
        free = 15

    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: FakeUsage())
    component = health.check_disk()
    assert component.status == health.WARN
    assert "85.0%" in component.detail


def test_check_disk_ok_below_threshold(monkeypatch):
    class FakeUsage:
        total = 100
        used = 40
        free = 60

    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: FakeUsage())
    component = health.check_disk()
    assert component.status == health.OK


def test_check_disk_missing_data_dir_is_ok(monkeypatch):
    def _raise(path):
        raise FileNotFoundError()

    monkeypatch.setattr(health.shutil, "disk_usage", _raise)
    component = health.check_disk()
    assert component.status == health.OK


@pytest.mark.asyncio
async def test_check_ytdlp_ok_when_no_degradation_flag():
    await redis_client.delete(YTDLP_DEGRADED_KEY)
    component = await health.check_ytdlp()
    assert component.status == health.OK


@pytest.mark.asyncio
async def test_check_ytdlp_warns_with_degradation_flag():
    await redis_client.set(YTDLP_DEGRADED_KEY, "2026-07-01T00:00:00+00:00")
    try:
        component = await health.check_ytdlp()
        assert component.status == health.WARN
        assert "2026-07-01" in component.detail
    finally:
        await redis_client.delete(YTDLP_DEGRADED_KEY)
