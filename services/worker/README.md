# Worker 服务

异步任务 Worker 通过数据库任务表领取 `PENDING` 任务，支持 `kb_index`、`eval` 和 `event_process`。

```bash
JOB_MODE=worker uv run python -m judicial_ai.worker
```

生产环境建议使用独立 Deployment 运行，副本数由任务积压量决定。
