# 运行手册

## 启动与升级

升级到带任务心跳的版本前，应先停止接收新任务并等待旧 worker 完成当前任务，
再执行数据库迁移和部署新版本。新版本要求生产环境开启资源 ACL 并使用代理推理。
启用 ACL 前，管理员必须核对并补齐已有知识库的应用或用户授权；
已有知识库不会自动获得创建者授权。新建知识库会为创建者授予 owner 权限。

```bash
uv run python -m alembic upgrade head
kubectl apply -k deploy/k8s/overlays/prod
kubectl -n judicial-ai rollout status deploy/judicial-ai-api
kubectl -n judicial-ai rollout status deploy/judicial-ai-worker
```

## 健康检查

- `/healthz`：进程存活。
- `/readyz`：数据库、Redis、向量、图谱、推理依赖。
- `/metrics`：Prometheus 指标。

## 常见故障

| 现象 | 排查 |
|---|---|
| readyz 返回 503 | 查看 `components` 中 database/redis/vector/graph/inference |
| 检索延迟升高 | 检查 Milvus 负载、rerank 超时、候选数量 |
| 任务积压 | 扩容 worker、检查 `job` 表 PENDING、查看失败原因 |
| 任务心跳超时 | 任务标记为 FAILED；先确认原执行是否仍在运行及外部副作用，再决定是否手动重试 |
| 授权失败 | 检查 grant 的 env/expires_at/data_scope 与应用租户 |
| 备份失败 | 检查 PVC、数据库凭据、Neo4j/MinIO 备份命令 |
| 索引状态不一致 | 管理员执行 `POST /api/v1/admin/indexes/reconcile?repair=false` 查看报告，确认后以 `repair=true` 清理 |
| 原件下载失败 | 检查 `document_version.storage_status`、对象卷挂载及响应中的完整性错误 |

## 回滚

1. `kubectl rollout undo deploy/judicial-ai-api`
2. 数据库迁移仅在确认兼容后回退；优先前向修复。
3. 恢复数据前先停止写入并确认 `RESTORE_CONFIRM=YES`。

## 原件与索引恢复

- 文件对象存储备份：`uv run python scripts/object_store_backup.py backup <目录>`。
- 文件对象存储恢复：`uv run python scripts/object_store_backup.py restore <目录>`。
- 集群备份恢复：挂载备份卷和可写原件卷后执行
  `RESTORE_CONFIRM=YES OBJECT_STORE_PATH=/data/objects restore.sh /backups/<时间戳>`。
  默认合并原件；灾难恢复需精确替换时显式设置 `OBJECT_RESTORE_MODE=replace`。
  MinIO 恢复还需设置 `RESTORE_MINIO=true` 和目标 `MINIO_ALIAS`。
- 文档默认软删除并进入保留期；保留期内可调用 `POST /api/v1/documents/{id}/restore`。
- 索引故障先执行只读对账，再执行修复；需要重建时调用
  `POST /api/v1/kbs/{kb_id}/indexes/{index_id}/recover`。
- 生产索引构建后进入待审批状态，批准后仍需单独设置 `prod` alias。
