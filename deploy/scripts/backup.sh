#!/usr/bin/env bash
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR/mysql" "$BACKUP_DIR/redis" "$BACKUP_DIR/neo4j" "$BACKUP_DIR/milvus"

parse_mysql() {
  local url="${DATABASE_URL:?DATABASE_URL is required}"
  local rest="${url#*://}"
  local credential="${rest%%@*}"
  local hostpart="${rest#*@}"
  MYSQL_USER="${credential%%:*}"
  MYSQL_PASSWORD="${credential#*:}"
  MYSQL_HOST="${hostpart%%:*}"
  local tail="${hostpart#*:}"
  MYSQL_PORT="${tail%%/*}"
  MYSQL_DATABASE="${tail#*/}"
  MYSQL_DATABASE="${MYSQL_DATABASE%%\?*}"
}

parse_redis() {
  local url="${REDIS_URL:-}"
  [ -z "$url" ] && return 0
  local rest="${url#*://}"
  local credential=""
  if [[ "$rest" == *"@"* ]]; then
    credential="${rest%%@*}"
    rest="${rest#*@}"
  fi
  if [ -n "$credential" ]; then
    REDIS_PASSWORD="${credential#*:}"
  fi
  REDIS_HOST="${rest%%:*}"
  local tail="${rest#*:}"
  REDIS_PORT="${tail%%/*}"
}

parse_mysql
parse_redis

DUMP_BIN="$(command -v mysqldump || command -v mariadb-dump || true)"
if [ -z "$DUMP_BIN" ]; then
  echo "[backup] mysqldump/mariadb-dump not found" >&2
  exit 1
fi
echo "[backup] mysql ${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}"
MYSQL_PWD="$MYSQL_PASSWORD" "$DUMP_BIN" \
  -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" \
  --single-transaction --routines --triggers --set-gtid-purged=OFF \
  "$MYSQL_DATABASE" | gzip -9 > "$BACKUP_DIR/mysql/${MYSQL_DATABASE}-${TS}.sql.gz"

if [ -n "${REDIS_HOST:-}" ]; then
  echo "[backup] redis ${REDIS_HOST}:${REDIS_PORT}"
  if [ -n "${REDIS_PASSWORD:-}" ]; then
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" --no-auth-warning --rdb "$BACKUP_DIR/redis/redis-${TS}.rdb"
  else
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --rdb "$BACKUP_DIR/redis/redis-${TS}.rdb"
  fi
fi

if [ -n "${NEO4J_BACKUP_CMD:-}" ]; then
  echo "[backup] neo4j via custom command"
  bash -c "$NEO4J_BACKUP_CMD"
else
  echo "[backup] neo4j skipped: set NEO4J_BACKUP_CMD for the target cluster"
fi

if [ -n "${MINIO_ALIAS:-}" ] && command -v mc >/dev/null 2>&1; then
  echo "[backup] milvus object storage mirror"
  mc mirror --overwrite "$MINIO_ALIAS" "$BACKUP_DIR/milvus/${TS}" || true
else
  echo "[backup] milvus skipped: configure MINIO_ALIAS and mc"
fi

find "$BACKUP_DIR" -type f -mtime +"$RETENTION_DAYS" -delete
printf '{"timestamp":"%s","mysql":"%s","redis":"%s"}\n' "$TS" "${MYSQL_DATABASE}" "${REDIS_HOST:-disabled}" > "$BACKUP_DIR/manifest-${TS}.json"
echo "[backup] complete ${TS}"
