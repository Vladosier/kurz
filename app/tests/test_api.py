"""API tests. Require a running Postgres + Redis.

Locally:  docker compose up -d db redis
          DATABASE_URL=postgresql://kurz:kurz@localhost:5432/kurz \
          REDIS_URL=redis://localhost:6379/0 pytest -q
In CI:    provided as GitHub Actions services (see .github/workflows/deploy.yml).
"""

import asyncpg
import pytest
import redis.asyncio as aioredis
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app import settings
from app.main import app
from app.worker import write_click


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def db_pool():
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
async def clean_queues():
    """Each test starts with empty click queues."""
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await r.delete(
        settings.CLICKS_QUEUE, settings.CLICKS_PROCESSING, settings.CLICKS_DEAD
    )
    yield
    await r.aclose()


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_healthz_accepts_head(client):
    # uptime monitors probe with HEAD; it must not 405.
    resp = await client.head("/healthz")
    assert resp.status_code == 200


async def test_redirect_accepts_head(client):
    resp = await client.post("/api/links", json={"url": "https://example.com/x"})
    code = resp.json()["code"]
    resp = await client.head(f"/{code}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/x"


async def test_click_is_persisted_end_to_end(client, db_pool):
    """The real path: redirect -> Redis queue -> worker -> Postgres -> stats.

    This is the test that was missing: it drives the worker and asserts the
    click COUNT changes, not merely that the response is shaped like a number.
    """
    resp = await client.post("/api/links", json={"url": "https://example.com/page"})
    assert resp.status_code == 201
    code = resp.json()["code"]

    resp = await client.get(f"/{code}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/page"

    # drain the one queued event through the actual worker function
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    raw = await r.rpop(settings.CLICKS_QUEUE)
    await r.aclose()
    assert raw is not None, "redirect did not enqueue a click event"
    await write_click(db_pool, raw)  # must not raise; must write exactly one row

    resp = await client.get(f"/api/links/{code}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks"] == 1  # the click actually landed
    assert body["daily"][-1]["clicks"] == 1  # and on today's bucket


async def test_redirect_survives_postgres_being_unreachable(client, monkeypatch):
    """Cache-aside promise: once a link is created, redirects work even if the
    database is unreachable. We swap the whole pool for one that raises, so a
    passing test proves the redirect never touched Postgres."""
    resp = await client.post("/api/links", json={"url": "https://example.com/live"})
    code = resp.json()["code"]

    from app import db

    class DeadPool:
        async def fetchrow(self, *a, **k):
            raise ConnectionError("postgres down")

    monkeypatch.setattr(db, "pool", DeadPool())

    resp = await client.get(f"/{code}")
    assert resp.status_code == 302  # served from the Redis cache
    assert resp.headers["location"] == "https://example.com/live"


async def test_unknown_code_is_404(client):
    resp = await client.get("/nope123")
    assert resp.status_code == 404


async def test_invalid_url_is_rejected(client):
    resp = await client.post("/api/links", json={"url": "not-a-url"})
    assert resp.status_code == 422
