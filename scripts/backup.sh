#!/usr/bin/env bash
# Nightly Postgres backup: pg_dump -> gzip -> keep the 7 most recent dumps.
#
# Cron example (crontab -e on the VPS):
#   0 3 * * * cd /opt/kurz && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Restore:
#   gunzip -c backups/kurz-YYYY-MM-DD-HHMM.sql.gz | \
#     docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

set -euo pipefail

cd "$(dirname "$0")/.."

set -a
source .env
set +a

mkdir -p backups
FILE="backups/kurz-$(date +%F-%H%M).sql.gz"

docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$FILE"

# rotation: keep the 7 newest dumps
ls -1t backups/kurz-*.sql.gz | tail -n +8 | xargs -r rm --

echo "$(date -Is) backup written: $FILE ($(du -h "$FILE" | cut -f1))"
