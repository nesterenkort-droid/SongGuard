"""Dead-man ping + yt-dlp canary/maintenance (network mocked, real Redis)."""

import pytest

from app.redis_client import redis_client
from app.services import ops


@pytest.mark.asyncio
async def test_dead_man_ping_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(ops.settings, "healthchecks_ping_url", None)
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)

    monkeypatch.setattr(ops.httpx, "AsyncClient", lambda **kw: FakeClient())
    await ops.dead_man_ping()
    assert calls == []


@pytest.mark.asyncio
async def test_dead_man_ping_calls_configured_url(monkeypatch):
    monkeypatch.setattr(ops.settings, "healthchecks_ping_url", "https://hc-ping.com/xyz")
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)

    monkeypatch.setattr(ops.httpx, "AsyncClient", lambda **kw: FakeClient())
    await ops.dead_man_ping()
    assert calls == ["https://hc-ping.com/xyz"]


@pytest.mark.asyncio
async def test_dead_man_ping_swallows_errors(monkeypatch):
    monkeypatch.setattr(ops.settings, "healthchecks_ping_url", "https://hc-ping.com/xyz")

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise ConnectionError("no network")

    monkeypatch.setattr(ops.httpx, "AsyncClient", lambda **kw: FailingClient())
    await ops.dead_man_ping()  # must not raise


@pytest.mark.asyncio
async def test_ytdlp_canary_skipped_when_unconfigured(monkeypatch):
    monkeypatch.setattr(ops.settings, "ytdlp_canary_url", None)
    ok, msg = await ops.run_ytdlp_canary()
    assert ok is True
    assert "не настроена" in msg


@pytest.mark.asyncio
async def test_ytdlp_maintenance_sets_and_clears_degraded_flag(monkeypatch):
    await redis_client.delete(ops.YTDLP_DEGRADED_KEY)
    monkeypatch.setattr(ops, "attempt_ytdlp_selfupdate", lambda: (True, "ok"))

    async def failing_canary():
        return False, "boom"

    monkeypatch.setattr(ops, "run_ytdlp_canary", failing_canary)
    result = await ops.run_ytdlp_maintenance()
    assert result["canary_ok"] is False
    flag = await redis_client.get(ops.YTDLP_DEGRADED_KEY)
    assert flag is not None
    first_timestamp = flag

    # A second failing run must NOT overwrite the original onset timestamp.
    result2 = await ops.run_ytdlp_maintenance()
    assert (await redis_client.get(ops.YTDLP_DEGRADED_KEY)) == first_timestamp
    assert result2["canary_ok"] is False

    async def passing_canary():
        return True, "ok"

    monkeypatch.setattr(ops, "run_ytdlp_canary", passing_canary)
    result3 = await ops.run_ytdlp_maintenance()
    assert result3["canary_ok"] is True
    assert (await redis_client.get(ops.YTDLP_DEGRADED_KEY)) is None

    await redis_client.delete(ops.YTDLP_DEGRADED_KEY)
