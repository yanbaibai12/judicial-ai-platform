#!/usr/bin/env bash
# 在 Linux 服务器上一键安装 Milvus standalone（供运维/管理员执行）
# 用法：bash install-milvus-remote.sh [安装目录，默认 /opt/milvus]
# 要求：Linux x86_64、可访问外网（或已配 docker 镜像源）；内存 >= 8G
set -euo pipefail

DIR="${1:-/opt/milvus}"
VERSION="v2.4.15"
COMPOSE_FILE="$DIR/docker-compose.yml"

mkdir -p "$DIR"

if command -v docker >/dev/null 2>&1; then
  echo "==> 检测到 Docker，使用 docker compose 方式安装"
  curl -sfL "https://github.com/milvus-io/milvus/releases/download/${VERSION}/milvus-standalone-docker-compose.yml" -o "$COMPOSE_FILE"
  docker compose -f "$COMPOSE_FILE" up -d
  echo "==> 等待健康检查..."
  for i in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" ps | grep -q "healthy"; then break; fi
    sleep 5
  done
  docker compose -f "$COMPOSE_FILE" ps
else
  echo "==> 未检测到 Docker，使用 binary 方式安装（内嵌 etcd + 本地存储）"
  curl -sfL "https://github.com/milvus-io/milvus/releases/download/${VERSION}/milvus-standalone-embed.sh" -o "$DIR/milvus-embed.sh"
  chmod +x "$DIR/milvus-embed.sh"
  (cd "$DIR" && sudo ./milvus-embed.sh start) || "$DIR/milvus-embed.sh" start
fi

echo
echo "==> 安装完成。验证端口："
echo "    curl http://$(hostname -I | awk '{print $1}'):19530/v2/vectordbs/list  (或 nc -zv <ip> 19530)"
echo
echo "==> 开发机 .env 中配置："
echo "    MILVUS_URI=http://<本机IP>:19530"
echo "    VECTOR_BACKEND=milvus"
echo
echo "==> 防火墙放行（如需要）："
echo "    sudo firewall-cmd --add-port=19530/tcp --permanent && sudo firewall-cmd --reload   # CentOS"
echo "    sudo ufw allow 19530/tcp                                                          # Ubuntu"
