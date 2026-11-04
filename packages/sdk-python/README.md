# Python SDK

SDK 随主包发布，入口为：

```python
from judicial_ai import Client, AsyncClient, JudicialAIError
```

```python
with Client("http://127.0.0.1:8080", "ak_xxx") as client:
    print(client.chat("llm.qwen3.chat", [{"role": "user", "content": "你好"}]))
    print(client.retrieve([1], "民间借贷利率上限", compress=True))
```

支持同步/异步、SSE 流式、超时、指数退避、请求 ID 与结构化错误。
