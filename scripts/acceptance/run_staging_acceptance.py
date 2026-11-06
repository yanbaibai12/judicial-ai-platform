"""staging 验收执行器：A01 / A03 / A05 / A08。

对应 `docs/13-后续开发步骤与最终验收.md` 第 5 节：

- A01 创建租户、应用、身份和凭证；注册、发布、授权并调用能力
- A03 创建知识版本、构建索引、切换别名并回滚
- A05 通过网关使用目标模型进行检索及回答，引用可定位
- A08 中断依赖，要求"明确错误或已批准降级"，恢复后一致

设计原则：
- 只做**可自动判定**的断言；需要法律/业务判断的部分只落证据，不冒充通过。
- 每条步骤记录 request_id，便于与访问日志、审计表对齐。
- 失败不中断整轮（除非依赖缺失），最终汇总为 FAIL 列表并返回非 0 退出码。

用法：
    python scripts/acceptance/run_staging_acceptance.py \
        --base-url http://staging.internal:8000 \
        --admin-token "$env:ADMIN_TOKEN" \
        --scenario all \
        --evidence-dir evidence

    # A08：在故障注入前后各跑一次，对比结果
    python scripts/acceptance/run_staging_acceptance.py \
        --scenario a08 --expect degraded --tag milvus-down
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

ADMIN_PERMISSIONS = [
    "app:invoke",
    "kb:read",
    "kb:write",
    "kb:approve",
    "kb:promote",
    "governance:read",
    "governance:write",
    "job:read",
    "job:write",
    "mcp:write",
]


@dataclass
class Step:
    sid: str
    title: str
    status: str
    detail: str = ""
    request_id: str | None = None


@dataclass
class Scenario:
    name: str
    steps: list[Step] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def add(
        self, sid: str, title: str, status: str, detail: str = "", request_id: str | None = None
    ) -> None:
        self.steps.append(Step(sid, title, status, detail, request_id))

    @property
    def failed(self) -> list[Step]:
        return [item for item in self.steps if item.status == FAIL]


class Api:
    def __init__(
        self,
        base_url: str = "",
        admin_token: str = "",
        timeout: float = 60.0,
        client: Any | None = None,
    ) -> None:
        # client 注入用于进程内（ASGI/TestClient）自检，避免依赖外部启动的 HTTP 服务。
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.admin_token = admin_token

    def close(self) -> None:
        self._client.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        merged = dict(headers or {})
        if token:
            merged["Authorization"] = f"Bearer {token}"
        return self._client.request(method, path, json=json_body, params=params, headers=merged)

    @staticmethod
    def data(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload.get("data", payload)

    @staticmethod
    def request_id(response: httpx.Response) -> str | None:
        return response.headers.get("X-Request-Id")


def _wait_job(
    api: Api, token: str, job_id: str, deadline_seconds: float = 300.0
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + deadline_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api.call("GET", f"/api/v1/jobs/{job_id}", token=token)
        if response.status_code != 200:
            return "HTTP_ERROR", last
        last = api.data(response) or {}
        status = str(last.get("status") or "")
        if status in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD"}:
            return status, last
        time.sleep(2)
    return "TIMEOUT", last


def _ensure_baseline(api: Api, scenario: Scenario) -> dict[str, Any]:
    """创建租户 + 授权应用 + 未授权应用，返回上下文。"""

    suffix = uuid.uuid4().hex[:8]
    tenant_code = f"acc-t-{suffix}"
    response = api.call(
        "POST",
        "/api/v1/admin/tenants",
        token=api.admin_token,
        json_body={"code": tenant_code, "name": tenant_code},
    )
    if response.status_code != 201:
        raise RuntimeError(f"创建租户失败：{response.status_code} {response.text[:200]}")
    tenant_id = int(Api.data(response)["id"])
    scenario.add("A01-01", "创建租户", PASS, f"tenant_id={tenant_id}", Api.request_id(response))

    def create_app(code: str, permissions: list[str]) -> tuple[int, str]:
        app_code = f"{code}-{suffix}"
        created = api.call(
            "POST",
            "/api/v1/admin/apps",
            token=api.admin_token,
            json_body={
                "tenant_id": tenant_id,
                "code": app_code,
                "name": app_code,
                "permissions": permissions,
            },
        )
        if created.status_code != 201:
            raise RuntimeError(f"创建应用失败：{created.status_code} {created.text[:200]}")
        app_id = int(Api.data(created)["id"])
        credential = api.call(
            "POST", f"/api/v1/admin/apps/{app_id}/credentials", token=api.admin_token
        )
        if credential.status_code != 201:
            raise RuntimeError(f"签发凭证失败：{credential.status_code} {credential.text[:200]}")
        return app_id, str(Api.data(credential)["api_key"])

    app_id, app_token = create_app("acc-app", ADMIN_PERMISSIONS)
    scenario.add("A01-02", "创建应用并签发凭证（已授权）", PASS, f"app_id={app_id}")
    _, denied_token = create_app("acc-deny", ["app:invoke", "kb:read"])
    scenario.add("A01-03", "创建应用并签发凭证（未授权）", PASS, "仅含 app:invoke/kb:read")

    return {
        "tenant_id": tenant_id,
        "app_id": app_id,
        "app_token": app_token,
        "denied_token": denied_token,
        "suffix": suffix,
    }


def _register_capability(api: Api, scenario: Scenario, ctx: dict[str, Any]) -> str:
    code = f"acc.llm.{ctx['suffix']}"
    created = api.call(
        "POST",
        "/api/v1/admin/capabilities",
        token=api.admin_token,
        json_body={
            "code": code,
            "name": "验收用对话能力",
            "type": "MODEL_CHAT",
            "owner": "acceptance",
            "version": "1.0.0",
            "protocol": "OPENAI",
            "model_name": "Qwen3-8B",
            "request_schema": {
                "type": "object",
                "required": ["messages"],
                "properties": {"messages": {"type": "array"}},
            },
            "response_schema": {"type": "object"},
            "tags": ["acceptance"],
        },
    )
    if created.status_code != 201:
        raise RuntimeError(f"注册能力失败：{created.status_code} {created.text[:200]}")
    scenario.add("A01-04", "注册能力", PASS, code, Api.request_id(created))

    published = api.call(
        "POST", f"/api/v1/admin/capabilities/{code}/versions/1.0.0/publish", token=api.admin_token
    )
    scenario.add(
        "A01-05",
        "发布能力版本",
        PASS if published.status_code == 200 else FAIL,
        f"HTTP {published.status_code}",
        Api.request_id(published),
    )

    granted = api.call(
        "POST",
        f"/api/v1/admin/capabilities/{code}/grants",
        token=api.admin_token,
        json_body={"app_id": ctx["app_id"]},
    )
    scenario.add(
        "A01-06",
        "授权能力给应用",
        PASS if granted.status_code in {200, 201} else FAIL,
        f"HTTP {granted.status_code}",
        Api.request_id(granted),
    )
    return code


def _invoke_matrix(api: Api, scenario: Scenario, ctx: dict[str, Any], code: str) -> None:
    payload = {"input": {"messages": [{"role": "user", "content": "请用一句话说明借款利率上限。"}]}}
    allowed = api.call(
        "POST", f"/api/v1/capabilities/{code}/invoke", token=ctx["app_token"], json_body=payload
    )
    scenario.add(
        "A01-07",
        "已授权应用调用能力",
        PASS if allowed.status_code == 200 else FAIL,
        f"HTTP {allowed.status_code}",
        Api.request_id(allowed),
    )
    scenario.evidence["A01_invoke_allowed"] = {
        "status_code": allowed.status_code,
        "body": Api.data(allowed),
    }

    denied = api.call(
        "POST", f"/api/v1/capabilities/{code}/invoke", token=ctx["denied_token"], json_body=payload
    )
    scenario.add(
        "A01-08",
        "未授权应用调用被拒绝",
        PASS if denied.status_code == 403 else FAIL,
        f"HTTP {denied.status_code}（期望 403）",
        Api.request_id(denied),
    )

    anonymous = api.call("POST", f"/api/v1/capabilities/{code}/invoke", json_body=payload)
    scenario.add(
        "A01-09",
        "匿名调用被拒绝",
        PASS if anonymous.status_code == 401 else FAIL,
        f"HTTP {anonymous.status_code}（期望 401）",
        Api.request_id(anonymous),
    )


def _create_kb(api: Api, scenario: Scenario, ctx: dict[str, Any], prefix: str) -> int:
    code = f"acc-kb-{prefix}-{ctx['suffix']}"
    response = api.call(
        "POST", "/api/v1/kbs", token=ctx["app_token"], json_body={"code": code, "name": code}
    )
    if response.status_code != 201:
        raise RuntimeError(f"创建知识库失败：{response.status_code} {response.text[:200]}")
    kb_id = int(Api.data(response)["id"])
    scenario.evidence.setdefault("kb_ids", []).append(kb_id)
    return kb_id


def _upload_text(
    api: Api, token: str, kb_id: int, title: str, text: str
) -> tuple[int, dict[str, Any]]:
    response = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/documents",
        token=token,
        json_body={"title": title, "text": text, "doc_type": "law"},
    )
    if response.status_code != 201:
        raise RuntimeError(f"写入文档失败：{response.status_code} {response.text[:200]}")
    return response.status_code, Api.data(response)


def _build_index(
    api: Api,
    scenario: Scenario,
    ctx: dict[str, Any],
    kb_id: int,
    version: str,
    sid_prefix: str,
    kb_version_id: int,
) -> dict[str, Any]:
    job = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/index-jobs",
        token=ctx["app_token"],
        params={"version": version},
        headers={"Idempotency-Key": f"{kb_id}-{version}"},
    )
    if job.status_code not in {200, 202}:
        raise RuntimeError(f"创建索引任务失败：{job.status_code} {job.text[:200]}")
    job_id = str(Api.data(job)["id"])
    scenario.add(f"{sid_prefix}-job", "提交索引任务", PASS, f"job_id={job_id}", Api.request_id(job))

    status, detail = _wait_job(api, ctx["app_token"], job_id)
    ok = status == "SUCCEEDED"
    scenario.add(
        f"{sid_prefix}-done",
        "索引任务完成",
        PASS if ok else FAIL,
        f"status={status}",
        Api.request_id(job),
    )
    if not ok:
        raise RuntimeError(f"索引任务未成功：{status} {(detail.get('error') or '')[:200]}")

    versions = api.call("GET", f"/api/v1/kbs/{kb_id}/versions", token=ctx["app_token"])
    indexes = (Api.data(versions) or {}).get("index_versions") or []
    # 索引版本名是自动生成的（idx-<时间戳>-<序号>），只能按 kb_version_id 关联；
    # 列表按 created_at 倒序，取第一条即最新一次构建。
    candidates = [item for item in indexes if int(item.get("kb_version_id") or 0) == kb_version_id]
    if not candidates:
        raise RuntimeError(f"索引列表中找不到知识版本 {version} 对应的索引")
    return candidates[0]


def _approve(
    api: Api, scenario: Scenario, ctx: dict[str, Any], kb_id: int, index: dict[str, Any]
) -> None:
    if str(index.get("status") or "") == "READY":
        scenario.add(
            "A03-approve",
            f"索引 {index['version']} 无需审批",
            SKIP,
            "status=READY（INDEX_REQUIRE_APPROVAL 关闭，staging 必须开启）",
        )
        return
    response = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/indexes/{index['id']}/approve",
        token=ctx["app_token"],
        json_body={"reason": "acceptance"},
    )
    scenario.add(
        "A03-approve",
        f"审批索引 {index['version']}",
        PASS if response.status_code == 200 else FAIL,
        f"HTTP {response.status_code}",
        Api.request_id(response),
    )


def _set_alias(
    api: Api,
    scenario: Scenario,
    ctx: dict[str, Any],
    kb_id: int,
    kb_version: str,
    index_version: str,
) -> None:
    response = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/aliases",
        token=ctx["app_token"],
        json_body={"alias": "prod", "kb_version": kb_version, "index_version": index_version},
    )
    scenario.add(
        "A03-alias",
        f"切换 prod 别名 -> {kb_version}/{index_version}",
        PASS if response.status_code == 200 else FAIL,
        f"HTTP {response.status_code}",
        Api.request_id(response),
    )


def _retrieve(
    api: Api, token: str, kb_id: int, query: str, top_k: int = 10
) -> tuple[int, dict[str, Any]]:
    response = api.call(
        "POST",
        "/v1/retrieve",
        token=token,
        json_body={"kb_ids": [kb_id], "query": query, "top_k": top_k, "with_citation": True},
    )
    return response.status_code, (Api.data(response) or {}) if response.status_code == 200 else {}


def _contains_token(payload: dict[str, Any], token: str) -> bool:
    """检索结果里是否出现指定内容标记。"""

    for item in payload.get("results") or []:
        if token in str(item.get("text") or ""):
            return True
        citation = item.get("citation") or {}
        if token in str(citation.get("snippet") or ""):
            return True
    return False


def scenario_a01(api: Api, scenario: Scenario, ctx: dict[str, Any]) -> None:
    code = _register_capability(api, scenario, ctx)
    _invoke_matrix(api, scenario, ctx, code)


def scenario_a03(api: Api, scenario: Scenario, ctx: dict[str, Any]) -> None:
    token = ctx["app_token"]
    kb_id = _create_kb(api, scenario, ctx, "a03")
    scenario.add("A03-01", "创建知识库", PASS, f"kb_id={kb_id}")

    alpha = f"ALPHA-{ctx['suffix']}"
    _upload_text(api, token, kb_id, "v1 文档", f"第一条：{alpha} 的借款利率不得超过四倍。")
    scenario.add("A03-02", "写入 v1 文档", PASS, f"token={alpha}")

    v1 = "v1-acc"
    created_v1 = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/versions",
        token=token,
        json_body={"version": v1, "publish": True},
    )
    scenario.add(
        "A03-03",
        "创建并发布知识版本 v1",
        PASS if created_v1.status_code == 201 else FAIL,
        f"HTTP {created_v1.status_code}",
        Api.request_id(created_v1),
    )
    kb_version_v1 = int((Api.data(created_v1) or {}).get("id") or 0)
    index_v1 = _build_index(api, scenario, ctx, kb_id, v1, "A03-04", kb_version_v1)
    _approve(api, scenario, ctx, kb_id, index_v1)
    _set_alias(api, scenario, ctx, kb_id, v1, str(index_v1["version"]))

    code, payload = _retrieve(api, token, kb_id, alpha)
    hits = payload.get("results") or []
    scenario.add(
        "A03-05",
        "v1 检索命中",
        PASS if code == 200 and hits else FAIL,
        f"HTTP {code}, hits={len(hits)}",
    )

    beta = f"BETA-{ctx['suffix']}"
    _upload_text(api, token, kb_id, "v2 新增文档", f"第二条：{beta} 的违约金上限需人工复核。")
    v2 = "v2-acc"
    created_v2 = api.call(
        "POST",
        f"/api/v1/kbs/{kb_id}/versions",
        token=token,
        json_body={"version": v2, "publish": True},
    )
    kb_version_v2 = int((Api.data(created_v2) or {}).get("id") or 0)
    index_v2 = _build_index(api, scenario, ctx, kb_id, v2, "A03-06", kb_version_v2)
    _approve(api, scenario, ctx, kb_id, index_v2)
    _set_alias(api, scenario, ctx, kb_id, v2, str(index_v2["version"]))

    code_v2, payload_v2 = _retrieve(api, token, kb_id, beta)
    hits_v2 = payload_v2.get("results") or []
    scenario.add(
        "A03-07",
        "v2 新内容可检索",
        PASS if code_v2 == 200 and _contains_token(payload_v2, beta) else FAIL,
        f"HTTP {code_v2}, hits={len(hits_v2)}",
    )

    _set_alias(api, scenario, ctx, kb_id, v1, str(index_v1["version"]))
    code_back, payload_back = _retrieve(api, token, kb_id, beta)
    hits_back = payload_back.get("results") or []
    # 只按"结果数=0"判断会误判：SQL 兜底路径没有相似度阈值，无关查询也会返回 top_k。
    # 回滚的正确判据是"新版本内容不再出现"。
    scenario.add(
        "A03-08",
        "别名回滚到 v1 后新内容不再出现",
        PASS if code_back == 200 and not _contains_token(payload_back, beta) else FAIL,
        (
            f"HTTP {code_back}, hits={len(hits_back)}, "
            f"contains_beta={_contains_token(payload_back, beta)}"
        ),
    )

    code_alpha, payload_alpha = _retrieve(api, token, kb_id, alpha)
    scenario.add(
        "A03-09",
        "回滚后旧内容仍可检索",
        PASS if code_alpha == 200 and (payload_alpha.get("results") or []) else FAIL,
        f"HTTP {code_alpha}",
    )

    scenario.evidence["A03_versions"] = {
        "v1_index": index_v1.get("version"),
        "v1_stats": index_v1.get("stats"),
        "v2_index": index_v2.get("version"),
        "v2_stats": index_v2.get("stats"),
    }
    ctx["kb_id"] = kb_id
    ctx["alpha"] = alpha


def scenario_a05(api: Api, scenario: Scenario, ctx: dict[str, Any], chat_model: str | None) -> None:
    token = ctx["app_token"]
    kb_id = ctx.get("kb_id") or _create_kb(api, scenario, ctx, "a05")
    if "alpha" not in ctx:
        alpha = f"ALPHA-{ctx['suffix']}"
        _upload_text(api, token, kb_id, "v1 文档", f"第一条：{alpha} 的借款利率不得超过四倍。")
        v1 = "v1-a05"
        created = api.call(
            "POST",
            f"/api/v1/kbs/{kb_id}/versions",
            token=token,
            json_body={"version": v1, "publish": True},
        )
        kb_version_v1 = int((Api.data(created) or {}).get("id") or 0)
        index = _build_index(api, scenario, ctx, kb_id, v1, "A05-setup", kb_version_v1)
        _approve(api, scenario, ctx, kb_id, index)
        _set_alias(api, scenario, ctx, kb_id, v1, str(index["version"]))
        ctx["alpha"] = alpha

    code, payload = _retrieve(api, token, kb_id, str(ctx["alpha"]))
    results = payload.get("results") or []
    scenario.add(
        "A05-01",
        "检索返回结果",
        PASS if code == 200 and results else FAIL,
        f"HTTP {code}, hits={len(results)}",
    )
    if not results:
        return

    first = results[0]
    citation = first.get("citation") or {}
    citation_status = payload.get("citation", {}).get("status")
    scenario.add(
        "A05-02",
        "引用状态为 VERIFIED",
        PASS if citation_status == "VERIFIED" else FAIL,
        f"status={citation_status}, counts={payload.get('citation', {}).get('counts')}",
    )

    source_url = citation.get("source_url")
    scenario.add("A05-03", "引用带原文定位地址", PASS if source_url else FAIL, str(source_url))
    if source_url:
        source = api.call("GET", str(source_url), token=token)
        detail = Api.data(source) or {}
        chunks = detail.get("chunks") or []
        matched = any(chunk.get("chunk_id") == first.get("chunk_id") for chunk in chunks)
        scenario.add(
            "A05-04",
            "引用可回溯到原文分块",
            PASS if source.status_code == 200 and matched else FAIL,
            f"HTTP {source.status_code}, matched={matched}",
            Api.request_id(source),
        )
        version = (detail.get("document_version") or {}).get("id")
        scenario.add(
            "A05-05",
            "引用版本与文档版本一致",
            PASS if version == citation.get("document_version_id") else FAIL,
            f"citation={citation.get('document_version_id')}, source={version}",
        )

    unknown = f"ZXQ{ctx['suffix']} 完全不存在的规定"
    code_none, payload_none = _retrieve(api, token, kb_id, unknown)
    none_hits = payload_none.get("results") or []
    none_status = (payload_none.get("citation") or {}).get("status")
    filtered = int((payload_none.get("usage") or {}).get("filtered_low_relevance") or 0)
    if code_none != 200:
        verdict, note = FAIL, f"HTTP {code_none}"
    elif not none_hits:
        verdict, note = PASS, "无依据查询未返回结果"
    elif filtered == 0:
        verdict = SKIP
        note = (
            f"未配置相关性阈值（RAG_MIN_DENSE_SCORE=0），无关查询仍返回 {len(none_hits)} 条；"
            "需业务确认阈值后重跑"
        )
    else:
        verdict = SKIP
        note = f"阈值已生效（过滤 {filtered} 条）但仍有 {len(none_hits)} 条进入 top_k，需人工判定"
    scenario.add("A05-06", "无依据问题不返回伪造证据", verdict, note)
    scenario.evidence["A05_no_evidence"] = {
        "http_status": code_none,
        "hits": len(none_hits),
        "filtered_low_relevance": filtered,
        "citation_status": none_status,
        "queries": [unknown],
    }

    if chat_model:
        chat = api.call(
            "POST",
            "/v1/chat/completions",
            token=token,
            json_body={
                "model": chat_model,
                "messages": [{"role": "user", "content": f"请根据规定回答：{unknown}"}],
                "max_tokens": 128,
            },
        )
        answer = ""
        if chat.status_code == 200:
            choices = (Api.data(chat) or {}).get("choices") or []
            if choices:
                answer = str((choices[0].get("message") or {}).get("content") or "")
        scenario.evidence["A05_chat"] = {
            "status_code": chat.status_code,
            "answer_preview": answer[:200],
        }
        scenario.add(
            "A05-07",
            "模型回答已记录（语义正确性需人工判定）",
            SKIP,
            "证据已落盘，需业务/法律复核",
            Api.request_id(chat),
        )


def probe_a08(api: Api, scenario: Scenario, ctx: dict[str, Any], expect: str) -> None:
    ready = api.call("GET", "/readyz")
    body = ready.json() if ready.content else {}
    components = body.get("components") or {}
    failures = body.get("critical_failures") or []
    down = sorted(
        name for name, item in components.items() if (item or {}).get("status") == "error"
    )
    scenario.evidence["A08_readyz"] = {
        "http_status": ready.status_code,
        "status": body.get("status"),
        "components": {name: (item or {}).get("status") for name, item in components.items()},
        "critical_failures": failures,
    }
    scenario.add(
        "A08-01",
        "采集 /readyz 快照",
        PASS if ready.status_code in {200, 503} else FAIL,
        f"HTTP {ready.status_code}, down={down}, critical={failures}",
        Api.request_id(ready),
    )

    if expect == "healthy":
        ok = ready.status_code == 200 and not down
        scenario.add(
            "A08-02",
            "全组件健康",
            PASS if ok else FAIL,
            f"down={down}",
        )
    elif expect == "degraded":
        # 通过标准：明确失败或明确降级，不得 200 且静默返回错误结果。
        explicit = ready.status_code == 503 or bool(failures)
        scenario.add(
            "A08-02",
            "故障被显式暴露",
            PASS if explicit else FAIL,
            f"HTTP {ready.status_code}, critical={failures}",
        )

    kb_id = ctx.get("kb_id")
    if kb_id:
        token = ctx["app_token"]
        code, payload = _retrieve(api, token, kb_id, str(ctx.get("alpha") or "借款利率"))
        usage = payload.get("usage") or {}
        graph = payload.get("graph") or {}
        acceptable = code in {200, 400, 503} and code != 500
        scenario.add(
            "A08-03",
            "业务请求返回明确结果而非 500",
            PASS if acceptable else FAIL,
            (
                f"HTTP {code}, recall_mode={usage.get('recall_mode')}, "
                f"graph_degraded={graph.get('degraded')}"
            ),
        )
        scenario.evidence["A08_retrieve"] = {
            "http_status": code,
            "recall_mode": usage.get("recall_mode"),
            "graph_degraded": graph.get("degraded"),
            "citation_status": (payload.get("citation") or {}).get("status"),
            "result_count": len(payload.get("results") or []),
        }


def render_markdown(scenarios: list[Scenario], meta: dict[str, Any]) -> str:
    lines = ["# staging 验收执行记录（自动生成）", ""]
    lines.append(f"- 执行时间：{meta['started_at']}")
    lines.append(f"- 目标地址：{meta['base_url']}")
    lines.append("- 执行器：`scripts/acceptance/run_staging_acceptance.py`")
    lines.append("")
    lines.append("> 本记录为自动判定结果，不能替代 `docs/13` 要求的人工复核、法律审核与签字。")
    lines.append("")
    for scenario in scenarios:
        total = len(scenario.steps)
        failed = len(scenario.failed)
        lines.append(f"## {scenario.name}：{'通过' if not failed else f'失败 {failed}/{total}'}")
        lines.append("")
        lines.append("| 步骤 | 判定 | 说明 | request_id |")
        lines.append("|---|---|---|---|")
        for step in scenario.steps:
            detail = step.detail.replace("|", "\\|")
            lines.append(
                f"| {step.sid} {step.title} | {step.status} | {detail} | {step.request_id or '-'} |"
            )
        lines.append("")
    return "\n".join(lines)


def _execute(api: Api, args: argparse.Namespace) -> list[Scenario]:
    scenarios: list[Scenario] = []
    needs_baseline = args.scenario != "a08"
    ctx: dict[str, Any] = {}
    if needs_baseline:
        bootstrap = Scenario("baseline")
        ctx = _ensure_baseline(api, bootstrap)
        scenarios.append(bootstrap)
        if args.scenario in {"all", "a01"}:
            a01 = Scenario("A01 能力注册与授权调用")
            scenario_a01(api, a01, ctx)
            scenarios.append(a01)
        if args.scenario in {"all", "a03"}:
            a03 = Scenario("A03 知识版本、索引审批与别名回滚")
            scenario_a03(api, a03, ctx)
            scenarios.append(a03)
        if args.scenario in {"all", "a05"}:
            a05 = Scenario("A05 检索引用可追溯")
            scenario_a05(api, a05, ctx, args.chat_model)
            scenarios.append(a05)
    if args.scenario in {"all", "a08"}:
        a08 = Scenario(f"A08 降级探测（expect={args.expect}）")
        probe_a08(api, a08, ctx, args.expect)
        scenarios.append(a08)
    return scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="staging 验收执行器（A01/A03/A05/A08）")
    parser.add_argument("--base-url", default="", help="目标地址；--asgi 时可省略")
    parser.add_argument(
        "--asgi",
        action="store_true",
        help="进程内直连 ASGI 应用（自检用，需本地数据库），不访问 --base-url",
    )
    parser.add_argument("--admin-token", default="")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "a01", "a03", "a05", "a08"],
        help="要执行的场景",
    )
    parser.add_argument("--chat-model", default=None, help="A05 可选：用于拒答探测的模型名")
    parser.add_argument("--expect", default="healthy", choices=["healthy", "degraded"])
    parser.add_argument("--tag", default="", help="证据标签，如 milvus-down")
    parser.add_argument("--evidence-dir", default="evidence")
    args = parser.parse_args(argv)

    if not args.admin_token:
        print(
            "缺少 --admin-token（staging 需保留 ALLOW_STATIC_ADMIN 或改用 OIDC 管理员令牌）",
            file=sys.stderr,
        )
        return 2

    started_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    exit_code = 0
    if args.asgi:
        from starlette.testclient import TestClient

        from judicial_ai.app import create_app

        args.base_url = args.base_url or "asgi://in-process"
        with TestClient(create_app()) as client:
            scenarios = _execute(Api(admin_token=args.admin_token, client=client), args)
    else:
        if not args.base_url:
            print("缺少 --base-url（或使用 --asgi 做进程内自检）", file=sys.stderr)
            return 2
        api = Api(args.base_url, args.admin_token)
        try:
            scenarios = _execute(api, args)
        finally:
            api.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"-{args.tag}" if args.tag else ""
    out_dir = Path(args.evidence_dir) / f"{stamp}{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        safe = scenario.name.split()[0].replace("/", "_").replace("（", "").replace("）", "")
        (out_dir / f"{safe}.json").write_text(
            json.dumps(
                {
                    "scenario": scenario.name,
                    "steps": [step.__dict__ for step in scenario.steps],
                    "evidence": scenario.evidence,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    (out_dir / "summary.md").write_text(
        render_markdown(
            scenarios,
            {
                "started_at": started_at,
                "base_url": args.base_url,
            },
        ),
        encoding="utf-8",
    )

    for scenario in scenarios:
        failed = scenario.failed
        print(f"{scenario.name}: {'PASS' if not failed else f'FAIL({len(failed)})'}")
        for step in failed:
            print(f"  - {step.sid} {step.title}: {step.detail}")
        if failed:
            exit_code = 1
    print(f"证据目录：{out_dir}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
