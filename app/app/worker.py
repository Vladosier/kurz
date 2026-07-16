"""Background worker: consumes click events from Redis, writes them to Postgres.

Runs as a separate container (see docker-compose.yml). If Postgres is briefly
unavailable, the event stays visible in the exception log and the loop retries —
redirects keep working the whole time because the API only touches Redis.
"""

import asyncio
import json
import logging
import signal

import asyncpg
import redis.asyncio as redis

from . import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kurz.worker")


async def handle_event(pool: asyncpg.Pool, raw: str) -> None:
    event = json.loads(raw)
    await pool.execute(
        """
        INSERT INTO clicks (link_id, clicked_at, referrer, user_agent, visitor_hash)
        VALUES ($1, $2::timestamptz, $3, $4, $5)
        """,
        event["link_id"],
        event["ts"],
        event.get("referrer"),
        event.get("user_agent"),
        event.get("visitor_hash"),
    )


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=3)
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    log.info("worker started (queue=%s)", settings.CLICKS_QUEUE)

    while not stop.is_set():
        try:
            item = await r.brpop(settings.CLICKS_QUEUE, timeout=5)
            if item is None:
                continue  # timeout — check the stop flag again
            _, raw = item
            await handle_event(pool, raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("failed to process click event, retrying in 2s")
            await asyncio.sleep(2)

    await r.aclose()
    await pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
