# Java SDK（骨架）

Java 17 零运行时依赖客户端，覆盖健康检查、Chat、Retrieve 与统一能力调用。JSON 字符串由调用方序列化；Spring Boot Starter 与 SSE 流式封装作为后续增强。

```java
var client = JudicialAiClient.builder("https://judicial-ai.example.com", "ak_xxx").build();
String answer = client.chat("""
  {"model":"llm.qwen3.chat","messages":[{"role":"user","content":"你好"}]}
""");
```
