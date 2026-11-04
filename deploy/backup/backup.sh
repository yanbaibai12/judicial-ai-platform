#!/bin/sh
# 司法 AI 中台备份脚本（PostgreSQL + Neo4j + MinIO）
# 设计目标：CronJob 内以非 root 运行，失败显式退出码，保留期自动清理。
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/${STAMP}"
mkdir -p "${TARGET}"

log() { echo "[backup ${STAMP}] $*"; }

# 1) PostgreSQL（平台元数据）
if [ -n "${DATABASE_URL:-}" ]; then
  if command -v pg_dump >/dev/null 2>&1; then
    log "dumping PostgreSQL ..."
    pg_dump "${DATABASE_URL}" --no-owner --format=custom \
      -f "${TARGET}/postgres.dump"
  else
    log "WARN: pg_dump 不可用，跳过 PostgreSQL"
  fi
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

# 4) 校验清单
find "${TARGET}" -type f -exec sha256sum {} \; >"${TARGET}/SHA256SUMS" || true
log "artifacts: $(find "${TARGET}" -type f | wc -l) files"

# 5) 保留期清理
find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" \
  -exec rm -rf {} \; 2>/dev/null || true

log "done -> ${TARGET}"
