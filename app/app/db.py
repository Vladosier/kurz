"""asyncpg connection pool shared by the API."""

import asyncpg

from . import settings

pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    global pool
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=10)
    return pool


async def disconnect() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None
