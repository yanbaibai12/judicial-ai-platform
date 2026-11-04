#!/usr/bin/env bash
set -euo pipefail

if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
  echo "Set RESTORE_CONFIRM=YES to acknowledge this destructive restore." >&2
  exit 1
fi

DUMP_FILE="${1:?usage: restore.sh /path/to/backup.sql.gz}"
url="${DATABASE_URL:?DATABASE_URL is required}"
rest="${url#*://}"
credential="${rest%%@*}"
hostpart="${rest#*@}"
user="${credential%%:*}"
password="${credential#*:}"
host="${hostpart%%:*}"
tail="${hostpart#*:}"
port="${tail%%/*}"
database="${tail#*/}"
database="${database%%\?*}"

RESTORE_BIN="$(command -v mysql || command -v mariadb || true)"
if [ -z "$RESTORE_BIN" ]; then
  echo "mysql/mariadb client not found" >&2
  exit 1
fi
echo "[restore] restoring ${database} from ${DUMP_FILE}"
gzip -dc "$DUMP_FILE" | MYSQL_PWD="$password" "$RESTORE_BIN" -h "$host" -P "$port" -u "$user" "$database"
echo "[restore] complete"
