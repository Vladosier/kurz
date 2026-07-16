"""kurz API — URL shortener with privacy-friendly click analytics.

Flow:
  POST /api/links              create a short link
  GET  /{code}                 302 redirect + click event pushed to Redis
  GET  /api/links/{code}/stats aggregated click stats (total + last 7 days)
  GET  /healthz                liveness: checks Postgres and Redis

Click events are written to Postgres asynchronously by app/worker.py,
so a slow database can never slow down a redirect.
"""

import hashlib
import json
import logging
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

from . import db, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kurz.api")

ALPHABET = string.ascii_letters + string.digits

# Sentinel cached for codes that don't exist, so repeated hits to a bogus code
# are answered from Redis instead of hammering Postgres (cache penetration).
CACHE_MISS_UNKNOWN = "\x00miss"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    app.state.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    log.info("api started (base_url=%s)", settings.BASE_URL)
    yield
    await app.state.redis.aclose()
    await db.disconnect()


app = FastAPI(title="kurz", lifespan=lifespan)


class LinkIn(BaseModel):
    url: HttpUrl


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(request: Request):
    await db.pool.fetchval("SELECT 1")
    await request.app.state.redis.ping()
    return {"status": "ok"}


@app.post("/api/links", status_code=201)
async def create_link(payload: LinkIn, request: Request):
    target = str(payload.url)
    # retry on the (unlikely) collision of a random code
    for _ in range(5):
        code = "".join(secrets.choice(ALPHABET) for _ in range(settings.CODE_LENGTH))
        try:
            await db.pool.execute(
                "INSERT INTO links (code, target_url) VALUES ($1, $2)",
                code,
                target,
            )
        except asyncpg.UniqueViolationError:
            continue
        # Warm the cache so redirects can be served without touching Postgres.
        # Best-effort: a cache miss just falls back to the database.
        try:
            await request.app.state.redis.set(
                settings.LINK_CACHE_PREFIX + code, target, ex=settings.LINK_CACHE_TTL
            )
        except Exception:
            log.exception("failed to warm link cache for %s", code)
        return {
            "code": code,
            "short_url": f"{settings.BASE_URL}/{code}",
            "target_url": target,
        }
    raise HTTPException(status_code=500, detail="could not generate a unique code")


@app.get("/api/links/{code}/stats")
async def link_stats(code: str):
    link = await db.pool.fetchrow(
        "SELECT id, target_url, created_at FROM links WHERE code = $1", code
    )
    if link is None:
        raise HTTPException(status_code=404, detail="unknown code")

    total = await db.pool.fetchval(
        "SELECT count(*) FROM clicks WHERE link_id = $1", link["id"]
    )
    rows = await db.pool.fetch(
        """
        SELECT d::date AS day, count(c.id) AS clicks
        FROM generate_series(now() - interval '6 days', now(), interval '1 day') AS d
        LEFT JOIN clicks c
               ON c.link_id = $1
              AND c.clicked_at::date = d::date
        GROUP BY day
        ORDER BY day
        """,
        link["id"],
    )
    return {
        "code": code,
        "target_url": link["target_url"],
        "created_at": link["created_at"].isoformat(),
        "total_clicks": total,
        "daily": [{"day": r["day"].isoformat(), "clicks": r["clicks"]} for r in rows],
    }


def _visitor_hash(request: Request) -> str:
    """Pseudonymous visitor id: sha256(salt|ip|ua). Raw IP is never stored."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")
    digest = hashlib.sha256(f"{settings.HASH_SALT}|{ip}|{ua}".encode()).hexdigest()
    return digest[:16]


async def _resolve_target(request: Request, code: str) -> str | None:
    """Return the target URL for a code, or None if it does not exist.

    Cache-first: a healthy Redis serves redirects even while Postgres is down.
    On a cache miss we read Postgres and backfill the cache. A distinct value
    (CACHE_MISS_UNKNOWN) is cached for unknown codes so a flood of hits to a
    bogus code cannot stampede the database.
    """
    key = settings.LINK_CACHE_PREFIX + code
    try:
        cached = await request.app.state.redis.get(key)
    except Exception:
        cached = None  # Redis down: fall through to Postgres
    if cached == CACHE_MISS_UNKNOWN:
        return None
    if cached:
        return cached

    row = await db.pool.fetchrow(
        "SELECT target_url FROM links WHERE code = $1", code
    )
    target = row["target_url"] if row else None
    try:
        await request.app.state.redis.set(
            key,
            target if target is not None else CACHE_MISS_UNKNOWN,
            ex=settings.LINK_CACHE_TTL,
        )
    except Exception:
        pass  # caching is best-effort
    return target


# NB: keep this route LAST — it matches any single path segment.
@app.api_route("/{code}", methods=["GET", "HEAD"])
async def redirect(code: str, request: Request):
    target = await _resolve_target(request, code)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown code")

    event = {
        "code": code,
        "ts": datetime.now(timezone.utc).isoformat(),
        "referrer": request.headers.get("referer"),
        "user_agent": request.headers.get("user-agent"),
        "visitor_hash": _visitor_hash(request),
    }
    try:
        await request.app.state.redis.lpush(settings.CLICKS_QUEUE, json.dumps(event))
    except Exception:
        # analytics must never break redirects
        log.exception("failed to enqueue click event")

    return RedirectResponse(target, status_code=302)
