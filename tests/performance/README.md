# 压测

```bash
# 健康检查基线
uv run python scripts/loadtest.py --scenario health --concurrency 8 --duration 15 --p95-threshold-ms 100

# OpenAI 兼容对话
uv run python scripts/loadtest.py --scenario chat --api-key ak_xxx \
  --model llm.qwen3.chat --concurrency 16 --duration 60 --p95-threshold-ms 2000

# 检索链路
uv run python scripts/loadtest.py --scenario retrieve --api-key ak_xxx \
  --kb-ids 1 2 --concurrency 16 --duration 60 --p95-threshold-ms 500 --compress
```

报告包含 P50/P95/P99、QPS、错误率与状态码分布。生产压测需使用脱敏数据和等价硬件，并记录模型版本、参数与 GPU 指标。
