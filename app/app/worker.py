"""Background worker: consumes click events from Redis, writes them to Postgres.

Runs as a separate container (see docker-compose.yml). Redirects never touch
this path, so if Postgres is briefly unavailable the redirect still works —
events simply wait in the queue until the database comes back.

Reliability: an event is moved to a processing list while it is being written,
so a crash mid-insert cannot drop it (leftovers are requeued on startup).
Events that can never succeed (malformed, or a code that no longer exists) go
to a dead-letter list instead of blocking the queue forever.
"""

import asyncio
import json
import logging
import signal
from datetime import datetime

import asyncpg
import redis.asyncio as redis

from . import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kurz.worker")

# Resolve code -> link_id inside the INSERT so the redirect path never has to
# read Postgres. If the code was deleted, SELECT yields no row and nothing is
# inserted (0 rows) — handled as a permanent failure by the caller.
INSERT_CLICK = """
    INSERT INTO clicks (link_id, clicked_at, referrer, user_agent, visitor_hash)
    SELECT id, $2, $3, $4, $5 FROM links WHERE code = $1
"""


class PermanentError(Exception):
    """Event will never succeed (malformed, or unknown code): dead-letter it."""


async def write_click(pool: asyncpg.Pool, raw: str) -> None:
    try:
        event = json.loads(raw)
        code = event["code"]
        # asyncpg binds timestamptz from datetime objects, not ISO strings.
        clicked_at = datetime.fromisoformat(event["ts"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"malformed event: {exc}") from exc

    result = await pool.execute(
        INSERT_CLICK,
        code,
        clicked_at,
        event.get("referrer"),
        event.get("user_agent"),
        event.get("visitor_hash"),
    )
    # "INSERT 0 0" means the code no longer exists — never going to succeed.
    if result.endswith(" 0"):
        raise PermanentError(f"click for unknown code {code!r}")


async def requeue_stale(r: redis.Redis) -> None:
    """Move anything left in the processing list back onto the queue.

    Runs once at startup: these are events a previous worker took but crashed
    before acking. Safe because writes are idempotent enough for analytics.
    """
    moved = 0
    while await r.lmove(settings.CLICKS_PROCESSING, settings.CLICKS_QUEUE, "LEFT", "RIGHT"):
        moved += 1
    if moved:
        log.info("requeued %d in-flight event(s) from a previous run", moved)


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=3)
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    await requeue_stale(r)
    log.info("worker started (queue=%s)", settings.CLICKS_QUEUE)

    while not stop.is_set():
        # Atomically pop the oldest event and park it in the processing list,
        # so it is never only "in memory" — a crash leaves it recoverable.
        raw = await r.blmove(
            settings.CLICKS_QUEUE, settings.CLICKS_PROCESSING, timeout=5,
            src="RIGHT", destination="LEFT",
        )
        if raw is None:
            continue  # idle timeout — re-check the stop flag

        try:
            await write_click(pool, raw)
        except PermanentError as exc:
            log.warning("dead-lettering event: %s", exc)
            await r.lpush(settings.CLICKS_DEAD, raw)
            await r.lrem(settings.CLICKS_PROCESSING, 1, raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transient (e.g. Postgres down): put it back and back off. The
            # event stays safe in the queue the whole time.
            log.exception("write failed, requeuing event; retrying in 2s")
            await r.lmove(
                settings.CLICKS_PROCESSING, settings.CLICKS_QUEUE, "LEFT", "RIGHT"
            )
            await asyncio.sleep(2)
        else:
            await r.lrem(settings.CLICKS_PROCESSING, 1, raw)  # ack: done

    await r.aclose()
    await pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
