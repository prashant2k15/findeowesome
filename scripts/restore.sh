#!/usr/bin/env bash
# Restore the newest (or a named) backup into the running Postgres container.
set -euo pipefail
FILE="${1:-$(ls -1t backups/*.sql.gz | head -n1)}"
echo "restoring $FILE"
gunzip -c "$FILE" | docker compose exec -T db psql -U "${POSTGRES_USER:-blf}" -d "${POSTGRES_DB:-blf}"
echo "done"
