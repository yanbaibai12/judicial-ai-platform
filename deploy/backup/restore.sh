#!/bin/sh
# 恢复脚本：从 backup.sh 产物恢复 MySQL、原件卷和可选 MinIO。
# 用法：RESTORE_CONFIRM=YES restore.sh /backups/<stamp>
set -eu

STAMP_DIR="${1:?用法: restore.sh <备份目录，如 /backups/20260101T020000Z>}"
[ -f "${STAMP_DIR}/SHA256SUMS" ] && { cd "${STAMP_DIR}" && sha256sum -c SHA256SUMS --quiet; }

if [ "${RESTORE_CONFIRM:-}" != "YES" ]; then
  echo "设置 RESTORE_CONFIRM=YES 后才允许恢复" >&2
  exit 1
fi

RESTORED=0
DUMP="${STAMP_DIR}/mysql.sql.gz"
if [ "${RESTORE_DATABASE:-true}" = "true" ] && [ -f "${DUMP}" ]; then
  : "${DATABASE_URL:?恢复 MySQL 需要 DATABASE_URL 环境变量}"
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
  echo "恢复 MySQL ${MYSQL_DATABASE} <- ${DUMP}"
  gzip -dc "${DUMP}" | MYSQL_PWD="${MYSQL_PASSWORD}" mariadb \
    -h "${MYSQL_HOST}" -P "${MYSQL_PORT}" -u "${MYSQL_USER}" "${MYSQL_DATABASE}"
  RESTORED=1
fi

ORIGINALS="${STAMP_DIR}/originals"
if [ "${RESTORE_ORIGINALS:-true}" = "true" ] && [ -d "${ORIGINALS}" ]; then
  : "${OBJECT_STORE_PATH:?恢复文件对象存储需要 OBJECT_STORE_PATH}"
  case "${OBJECT_STORE_PATH}" in
    /*) ;;
    *) echo "OBJECT_STORE_PATH 必须是绝对路径" >&2; exit 1 ;;
  esac
  [ "${OBJECT_STORE_PATH}" != "/" ] || {
    echo "拒绝将根目录作为 OBJECT_STORE_PATH" >&2
    exit 1
  }
  mkdir -p "${OBJECT_STORE_PATH}"
  if [ "${OBJECT_RESTORE_MODE:-merge}" = "replace" ]; then
    find "${OBJECT_STORE_PATH}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  elif [ "${OBJECT_RESTORE_MODE:-merge}" != "merge" ]; then
    echo "OBJECT_RESTORE_MODE 只能是 merge 或 replace" >&2
    exit 1
  fi
  cp -a "${ORIGINALS}/." "${OBJECT_STORE_PATH}/"
  echo "原件卷恢复完成 -> ${OBJECT_STORE_PATH}"
  RESTORED=1
fi

MINIO_BACKUP="${STAMP_DIR}/minio"
if [ "${RESTORE_MINIO:-false}" = "true" ] && [ -d "${MINIO_BACKUP}" ]; then
  : "${MINIO_ALIAS:?恢复 MinIO 需要 MINIO_ALIAS}"
  command -v mc >/dev/null 2>&1 || {
    echo "恢复 MinIO 需要 mc 命令" >&2
    exit 1
  }
  mc mirror --overwrite --remove "${MINIO_BACKUP}" "${MINIO_ALIAS}"
  echo "MinIO 恢复完成 -> ${MINIO_ALIAS}"
  RESTORED=1
fi

[ "${RESTORED}" -eq 1 ] || {
  echo "没有找到或启用任何可恢复组件" >&2
  exit 1
}
echo "恢复完成"
