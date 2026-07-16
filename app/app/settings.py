"""App configuration, read from environment variables.

DATABASE_URL / REDIS_URL can be set directly (used in CI and local tests);
otherwise the DSN is built from POSTGRES_* pieces (used in docker compose).
"""

import os


def _database_url() -> str:
    if url := os.getenv("DATABASE_URL"):
        return url
    user = os.getenv("POSTGRES_USER", "kurz")
    password = os.getenv("POSTGRES_PASSWORD", "kurz")
    db = os.getenv("POSTGRES_DB", "kurz")
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _database_url()
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Base URL used to build the short links returned by the API.
BASE_URL = os.getenv("BASE_URL", "http://localhost").rstrip("/")

# Salt for visitor hashing — analytics without storing raw IPs.
HASH_SALT = os.getenv("HASH_SALT", "dev-salt-change-me")

CLICKS_QUEUE = "kurz:clicks"
# Events are moved here while being written, so a crash mid-insert cannot lose
# them; anything left over is requeued on the next worker start.
CLICKS_PROCESSING = "kurz:clicks:processing"
# Events that could not be written after MAX_ATTEMPTS, or that are malformed.
CLICKS_DEAD = "kurz:clicks:dead"

# code -> target cache. This is what keeps redirects alive while Postgres is
# down, so it is load-bearing, not an optimisation.
LINK_CACHE_PREFIX = "kurz:link:"
LINK_CACHE_TTL = 86400  # seconds

CODE_LENGTH = 7
