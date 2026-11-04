#!/bin/sh
# 恢复脚本：从 backup.sh 产物恢复 PostgreSQL。
# 用法：restore.sh /backups/<stamp>  （Neo4j/MinIO 恢复请按集群文档执行）
set -eu

STAMP_DIR="${1:?用法: restore.sh <备份目录，如 /backups/20260101T020000Z>}"
[ -f "${STAMP_DIR}/SHA256SUMS" ] && { cd "${STAMP_DIR}" && sha256sum -c SHA256SUMS --quiet; }

DUMP="${STAMP_DIR}/postgres.dump"
[ -f "${DUMP}" ] || { echo "未找到 ${DUMP}"; exit 1; }

: "${DATABASE_URL:?需要 DATABASE_URL 环境变量}"
echo "恢复 PostgreSQL <- ${DUMP}（目标库将被覆盖，确认后继续）"
pg_restore --clean --if-exists --no-owner --dbname "${DATABASE_URL}" "${DUMP}"
echo "恢复完成"
