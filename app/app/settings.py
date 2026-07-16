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
CODE_LENGTH = 7
