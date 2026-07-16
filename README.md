# kurz.

![CI/CD](https://github.com/Vladosier/kurz/actions/workflows/deploy.yml/badge.svg)

Self-hosted URL shortener with privacy-friendly click analytics.
*kurz* is German for "short" — and it's built the way EU clients like it: no cookies, no third-party trackers, visitor IPs are hashed and never stored.

**Live demo:** https://kurzvlad.duckdns.org

## Why this exists

A small production system, end to end: a real multi-service architecture (API, background worker, database, queue, reverse proxy), deployed on a single VPS with Docker Compose, tested and shipped automatically by a CI/CD pipeline, and backed up on a schedule. Every piece here maps to a task I do for clients: VPS setup, containerization, HTTPS, CI/CD pipelines, monitoring-ready services, backups.

## Architecture

```mermaid
flowchart LR
    U[Visitor] -->|HTTPS| C[Caddy]
    C -->|"/"| S[Static landing page]
    C -->|"/api/*  &  /:code"| A[FastAPI]
    A -->|read/write links| P[(PostgreSQL)]
    A -->|LPUSH click event| R[(Redis)]
    W[Worker] -->|BRPOP| R
    W -->|INSERT clicks| P
```

The design decision that matters: **a redirect never waits for the database.**
Resolution is cache-first — the `code → target` mapping is cached in Redis when
a link is created, so a redirect is served straight from Redis and only falls
back to Postgres on a cache miss. The click itself is recorded asynchronously:
the API pushes a JSON event to a Redis queue and returns `302` immediately,
while a separate worker drains the queue into Postgres.

So if Postgres is slow or briefly down, existing links keep redirecting and
clicks queue up until the database returns. The worker parks each event in a
processing list while writing it, so a crash mid-insert can't lose it, and
events that can never succeed are moved to a dead-letter list rather than
blocking the queue. (Creating *new* links still needs Postgres.)

| Service | Tech | Role |
|---|---|---|
| `caddy` | Caddy 2 | TLS (automatic Let's Encrypt), routing, static files |
| `api` | FastAPI / Python 3.12 | create links, redirect, stats endpoint, `/healthz` |
| `worker` | same image, different command | consume click events, write to Postgres |
| `db` | PostgreSQL 16 | links + clicks, schema in `db/init.sql` |
| `redis` | Redis 7 (AOF on) | click-event queue |

## API

```
POST /api/links                  {"url": "https://..."}   → 201 {code, short_url, target_url}
GET  /{code}                     → 302 redirect (click event enqueued)
GET  /api/links/{code}/stats     → {total_clicks, daily: last 7 days}
GET  /healthz                    → checks Postgres + Redis
```

## Run it locally

Requires Docker.

```bash
git clone https://github.com/Vladosier/kurz && cd kurz
cp .env.example .env                 # defaults are fine locally
# in .env set: DOMAIN=http://localhost  BASE_URL=http://localhost
docker compose up -d --build
open http://localhost
```

Run the tests (they need Postgres and Redis, the compose ones work):

```bash
docker compose up -d db redis
pip install -r app/requirements.txt -r app/requirements-dev.txt
DATABASE_URL=postgresql://kurz:kurz@localhost:5432/kurz \
REDIS_URL=redis://localhost:6379/0 \
pytest app -q
```

> Note: the compose file publishes no DB/Redis ports by default (they live on the
> internal network). For local test runs either add a `ports:` mapping
> temporarily or run pytest inside the network.

## Deploy to a VPS

Tested on a basic Ubuntu VPS (e.g. Hetzner CX22).

1. **DNS** — create an `A` record for your (sub)domain pointing to the VPS IP.
2. **Server prep** — install Docker (`curl -fsSL https://get.docker.com | sh`),
   enable the firewall: `ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`.
3. **Project** —
   ```bash
   sudo mkdir -p /opt/kurz && sudo chown $USER /opt/kurz
   git clone https://github.com/Vladosier/kurz /opt/kurz && cd /opt/kurz
   cp .env.example .env && nano .env    # domain, strong DB password, random HASH_SALT
   docker compose up -d --build         # first start builds locally; CI ships images afterwards
   ```
4. Open `https://your-domain` — Caddy obtains the TLS certificate automatically
   on the first request (DNS must already resolve).

## CI/CD

Every push to `main` runs `.github/workflows/deploy.yml`:

1. **test** — spins up Postgres + Redis as services, applies `db/init.sql`, runs pytest;
2. **build-and-push** — builds the Docker image and pushes it to GitHub Container Registry
   (`ghcr.io/vladosier/kurz`, tags `latest` + commit SHA);
3. **deploy** — SSHes into the VPS, `docker compose pull && up -d`.

Repository secrets required: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`
(a dedicated deploy key: `ssh-keygen -t ed25519 -f deploy_key`, put the public
half into `~/.ssh/authorized_keys` on the VPS, the private half into the secret).

Make the repo **public** so the GHCR image is public too; otherwise run
`docker login ghcr.io` on the VPS once with a read-only PAT.

## Backups

`scripts/backup.sh` dumps Postgres, gzips it and keeps the 7 newest dumps.

```bash
# nightly at 03:00
crontab -e
0 3 * * * cd /opt/kurz && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Restore:

```bash
gunzip -c backups/kurz-YYYY-MM-DD-HHMM.sql.gz | \
  docker compose exec -T db psql -U kurz -d kurz
```

## Security & privacy notes

- Containers run as a non-root user; DB and Redis are not exposed to the internet.
- Raw visitor IPs are never stored — only `sha256(salt|ip|user-agent)`, truncated.
- No cookies, no client-side tracking scripts.
- Secrets live in `.env` (git-ignored) and GitHub Actions secrets.

## Roadmap

- [ ] Rate limiting on link creation
- [ ] Prometheus metrics + Grafana dashboard (`/metrics`)
- [ ] Custom link codes and expiry dates
- [ ] Ansible playbook for one-command server provisioning
