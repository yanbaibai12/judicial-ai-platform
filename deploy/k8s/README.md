# Kubernetes 部署

## 前置条件

- Kubernetes 1.26+
- Kustomize 5+
- ingress-nginx、Prometheus Operator（可选）
- 外部 MySQL、Redis、Milvus、Neo4j、vLLM（生产不建议与应用同 Pod）

## 步骤

```bash
docker build -t registry.example.com/judicial-ai-platform:0.2.0 .
docker build -f deploy/backup/Dockerfile -t registry.example.com/judicial-ai-backup:0.2.0 .
docker push registry.example.com/judicial-ai-platform:0.2.0
docker push registry.example.com/judicial-ai-backup:0.2.0

kubectl apply -f deploy/k8s/base/namespace.yaml
kubectl -n judicial-ai create secret generic judicial-ai-secrets \
  --from-env-file=.env.prod
kubectl apply -k deploy/k8s/overlays/prod
kubectl -n judicial-ai wait --for=condition=complete job/judicial-ai-migrate-0-2-0 --timeout=300s
kubectl -n judicial-ai rollout status deploy/judicial-ai-api
kubectl -n judicial-ai rollout status deploy/judicial-ai-worker
```

`secret.example.yaml` 仅作为字段说明，不要提交真实值。生产建议使用 External Secrets + Vault/KMS。

## 验证

```bash
kubectl -n judicial-ai get pods,svc,ingress,hpa,pdb
kubectl -n judicial-ai port-forward svc/judicial-ai-api 8080:80
curl -fsS http://127.0.0.1:8080/readyz
curl -fsS http://127.0.0.1:8080/metrics | head
```

## 注意

- `AUTO_CREATE_SCHEMA=false`，必须先执行 migration Job。
- 生产镜像使用非 root、只读根文件系统；需要写入的 `/tmp` 使用 emptyDir。
- Worker 当前默认单副本；扩容时需为每个 Pod 设置唯一 `NODE_ID`。
- 备份 CronJob 需要 `DATABASE_URL`/`REDIS_URL` 与备份 PVC；Neo4j、Milvus 需按实际集群配置 `NEO4J_BACKUP_CMD`/`MINIO_ALIAS`。
- 定期执行恢复演练并记录 RTO/RPO。

## 可观测性

安装 Prometheus Operator 后单独应用告警规则：

```bash
kubectl apply -f deploy/k8s/observability/prometheus-rules.yaml
```

