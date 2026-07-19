"""Shared test fixtures."""

import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import service as auth_service
from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def fake_nonce_store(monkeypatch):
    """Replace the Redis-backed nonce store with an in-memory dict.

    Keeps auth tests hermetic and avoids the shared global Redis client binding to a
    per-test event loop. `confirm_start` looks these up in module globals, so patching
    the module attributes is enough.
    """
    store: dict[str, dict] = {}

    async def create_nonce(mode, invite_token=None):
        nonce = secrets.token_urlsafe(8)
        store[nonce] = {
            "status": "pending",
            "mode": mode,
            "invite_token": invite_token,
            "user_id": None,
        }
        return nonce

    async def get_nonce(nonce):
        return store.get(nonce)

    async def save_nonce(nonce, payload):
        store[nonce] = payload

    monkeypatch.setattr(auth_service, "create_nonce", create_nonce)
    monkeypatch.setattr(auth_service, "get_nonce", get_nonce)
    monkeypatch.setattr(auth_service, "_save_nonce", save_nonce)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session():
    """A session bound to a transaction that is rolled back after the test.

    Uses the real (migrated) Postgres from the compose network. `commit()` inside the
    code under test lands in a savepoint, so nothing leaks between tests.
    """
    engine = create_async_engine(settings.database_url)
    conn = await engine.connect()
    trans = await conn.begin()
    Session = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session = Session()
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def cleanup_redis():
    """Close the global Redis client's connection pool after each test
    to prevent event loop mismatch errors.
    """
    yield
    from app.redis_client import redis_client
    await redis_client.aclose()
