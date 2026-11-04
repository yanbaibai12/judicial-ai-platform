#!/bin/sh
# 司法 AI 中台备份脚本（MySQL + Neo4j + 对象存储）
# 设计目标：CronJob 内以非 root 运行，失败显式退出码，保留期自动清理。
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/${STAMP}"
mkdir -p "${TARGET}"

log() { echo "[backup ${STAMP}] $*"; }

# 1) MySQL（平台元数据）
if [ -n "${DATABASE_URL:-}" ]; then
  REST="${DATABASE_URL#*://}"
  CREDENTIAL="${REST%%@*}"
  HOSTPART="${REST#*@}"
  MYSQL_USER="${CREDENTIAL%%:*}"
  MYSQL_PASSWORD="${CREDENTIAL#*:}"
  MYSQL_HOST="${HOSTPART%%:*}"
  TAIL="${HOSTPART#*:}"
  MYSQL_PORT="${TAIL%%/*}"
  MYSQL_DATABASE="${TAIL#*/}"
  MYSQL_DATABASE="${MYSQL_DATABASE%%\?*}"
  log "dumping MySQL ${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE} ..."
  MYSQL_PWD="${MYSQL_PASSWORD}" mariadb-dump \
    -h "${MYSQL_HOST}" -P "${MYSQL_PORT}" -u "${MYSQL_USER}" \
    --single-transaction --routines --triggers \
    "${MYSQL_DATABASE}" | gzip -9 >"${TARGET}/mysql.sql.gz"
fi

# 2) Neo4j（图数据）
# NEO4J_BACKUP_CMD 允许自定义（如 kubectl exec / neo4j-admin database dump），
# 其输出（或产生的 dump 文件路径）应写到 $TARGET/neo4j.dump
if [ -n "${NEO4J_BACKUP_CMD:-}" ]; then
  log "running NEO4J_BACKUP_CMD ..."
  sh -c "${NEO4J_BACKUP_CMD}"
elif [ -n "${NEO4J_URI:-}" ]; then
  log "WARN: NEO4J_BACKUP_CMD 未配置，跳过 Neo4j（配置 dump 命令后再启用）"
fi

# 3) MinIO / 对象存储（原始文档）
if [ -n "${MINIO_ALIAS:-}" ] && command -v mc >/dev/null 2>&1; then
  log "mirroring MinIO bucket ..."
  mkdir -p "${TARGET}/minio"
  mc mirror --overwrite "${MINIO_ALIAS}" "${TARGET}/minio"
else
  log "WARN: MINIO_ALIAS 或 mc 不可用，跳过对象存储"
fi

# 文件对象存储需要把 OBJECT_STORE_PATH 所在卷同时挂载到备份任务。
if [ "${OBJECT_STORE_BACKEND:-}" = "filesystem" ] && [ -d "${OBJECT_STORE_PATH:-}" ]; then
  log "backing up filesystem object store ..."
  mkdir -p "${TARGET}/originals"
  cp -a "${OBJECT_STORE_PATH}/." "${TARGET}/originals/"
fi

# 4) 校验清单（清单本身不得参与哈希）
find "${TARGET}" -type f ! -name SHA256SUMS -exec sha256sum {} \; \
  >"${TARGET}/SHA256SUMS"
log "artifacts: $(find "${TARGET}" -type f | wc -l) files"

# 5) 保留期清理
find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" \
  -exec rm -rf {} \; 2>/dev/null || true

log "done -> ${TARGET}"
