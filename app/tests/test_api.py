"""API tests. Require a running Postgres + Redis.

Locally:  docker compose up -d db redis
          DATABASE_URL=postgresql://kurz:kurz@localhost:5432/kurz \
          REDIS_URL=redis://localhost:6379/0 pytest -q
In CI:    provided as GitHub Actions services (see .github/workflows/deploy.yml).
"""

import pytest
import redis.asyncio as aioredis
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app import settings
from app.main import app


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_create_redirect_and_stats(client):
    # create
    resp = await client.post("/api/links", json={"url": "https://example.com/page"})
    assert resp.status_code == 201
    data = resp.json()
    code = data["code"]
    assert len(code) == settings.CODE_LENGTH
    assert data["short_url"].endswith(f"/{code}")

    # redirect
    resp = await client.get(f"/{code}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/page"

    # the click event landed in the Redis queue
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        assert await r.llen(settings.CLICKS_QUEUE) >= 1
    finally:
        await r.aclose()

    # stats endpoint responds with the expected shape
    resp = await client.get(f"/api/links/{code}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == code
    assert isinstance(body["total_clicks"], int)
    assert len(body["daily"]) == 7


async def test_unknown_code_is_404(client):
    resp = await client.get("/nope123")
    assert resp.status_code == 404


async def test_invalid_url_is_rejected(client):
    resp = await client.post("/api/links", json={"url": "not-a-url"})
    assert resp.status_code == 422
