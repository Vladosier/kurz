-- Schema is applied automatically on first container start
-- (mounted into /docker-entrypoint-initdb.d/).

CREATE TABLE IF NOT EXISTS links (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    target_url  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clicks (
    id            BIGSERIAL PRIMARY KEY,
    link_id       BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    clicked_at    TIMESTAMPTZ NOT NULL,
    referrer      TEXT,
    user_agent    TEXT,
    visitor_hash  TEXT  -- sha256(salt|ip|ua), first 16 chars; raw IPs are never stored
);

CREATE INDEX IF NOT EXISTS idx_clicks_link_time ON clicks (link_id, clicked_at);
