#!/bin/bash
# backup_postgres.sh — daily pg_dump of the trading bot Postgres container.
#
# Crontab:
#   0 23 * * * /root/hft-trading-algo/scripts/backup_postgres.sh >> /var/log/hft-postgres-backup.log 2>&1
#
# What it does:
#   - Runs pg_dump inside the running hft-postgres container
#   - Writes a gzipped SQL file to /root/backups/postgres/
#   - Prunes anything older than 14 days
#
# Restore:
#   zcat /root/backups/postgres/hft-YYYY-MM-DD-HHMM.sql.gz \
#     | docker exec -i hft-postgres psql -U hft -d hft

set -euo pipefail

CONTAINER="${PG_CONTAINER:-hft-postgres}"
DB="${PG_DB:-hft}"
USER="${PG_USER:-hft}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups/postgres}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

stamp="$(date -u +'%Y-%m-%d-%H%M%S')"
out="${BACKUP_DIR}/hft-${stamp}.sql.gz"

docker exec "$CONTAINER" pg_dump -U "$USER" -d "$DB" --no-owner --clean --if-exists \
    | gzip -9 > "$out"

bytes="$(stat -c%s "$out" 2>/dev/null || echo 0)"
echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ')  wrote $out  (${bytes} bytes)"

# Prune anything older than RETAIN_DAYS
find "$BACKUP_DIR" -type f -name 'hft-*.sql.gz' -mtime +"$RETAIN_DAYS" -delete \
    | while read -r f; do
        echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ')  pruned $f"
    done

# Print summary
remaining=$(find "$BACKUP_DIR" -type f -name 'hft-*.sql.gz' | wc -l)
total_size=$(du -sh "$BACKUP_DIR" 2>/dev/null | awk '{print $1}')
echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ')  ${remaining} backup(s) retained, total ${total_size:-0}"
