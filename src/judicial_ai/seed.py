from __future__ import annotations

import json
from typing import Any

from .config import get_settings
from .db import SessionLocal, init_db
from .ids import new_id
from .models import (
    App,
    AppCredential,
    Capability,
    CapabilityGrant,
    CapabilityVersion,
    Document,
    IndexVersion,
    KbVersion,
    KnowledgeBase,
    McpServer,
    McpTool,
    McpToolGrant,
    Tenant,
)
from .schemas import (
    CapabilityCreate,
    DocumentCreate,
    KbCreate,
    McpServerCreate,
    McpToolCreate,
)
from .security import generate_api_key
from .services.capabilities import create_capability, create_version, publish_version
from .services.mcp_hub import create_server, create_tool, grant_tool
from .services.retrieval import (
    add_document,
    create_kb,
    create_kb_version,
    index_kb,
    set_alias,
)

DEMO_TENANT_CODE = "demo"
DEMO_APP_CODE = "demo-app"
DEMO_KB_CODE = "legal-demo"


def _capability_specs() -> list[CapabilityCreate]:
    messages_schema = {
        "type": "object",
        "required": ["messages"],
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["role", "content"],
                    "properties": {
                        "role": {"type": "string"},
                        "content": {},
                    },
                },
            }
        },
    }
    return [
        CapabilityCreate(
            code="llm.qwen3.chat",
            name="司法通用对话模型",
            type="MODEL_CHAT",
            owner="ai-platform",
            description="Qwen3-8B 基座，承载通用司法问答与文书辅助",
            version="1.0.0",
            protocol="OPENAI",
            endpoint="http://vllm-chat:8000/v1",
            model_name="Qwen3-8B",
            request_schema=messages_schema,
            response_schema={"type": "object"},
            tags=["chat", "judicial", "qwen3"],
        ),
        CapabilityCreate(
            code="llm.qwen3.classify",
            name="案由分类模型",
            type="MODEL_CHAT",
            owner="ai-platform",
            description="Qwen3-8B + 案由分类 LoRA Adapter",
            version="1.0.0",
            protocol="OPENAI",
            model_name="Qwen3-8B",
            adapter_alias="judicial-case-classify",
            request_schema=messages_schema,
            response_schema={"type": "object"},
            tags=["chat", "classification", "lora"],
        ),
        CapabilityCreate(
            code="embedding.bge-m3",
            name="BGE-M3 文本向量",
            type="MODEL_EMBEDDING",
            owner="ai-platform",
            description="公共检索向量化能力",
            version="1.0.0",
            protocol="OPENAI",
            model_name="bge-m3",
            request_schema={
                "type": "object",
                "required": ["input"],
                "properties": {"input": {"oneOf": [{"type": "string"}, {"type": "array"}]}},
            },
            response_schema={"type": "object"},
            tags=["embedding", "retrieval"],
        ),
        CapabilityCreate(
            code="rerank.bge-m3",
            name="BGE-Reranker 精排",
            type="MODEL_RERANK",
            owner="ai-platform",
            description="公共检索候选精排能力",
            version="1.0.0",
            protocol="PLATFORM",
            model_name="bge-reranker-v2-m3",
            request_schema={
                "type": "object",
                "required": ["query", "documents"],
                "properties": {
                    "query": {"type": "string"},
                    "documents": {"type": "array", "items": {"type": "string"}},
                },
            },
            response_schema={"type": "object"},
            tags=["rerank", "retrieval"],
        ),
        CapabilityCreate(
            code="rag.legal.retrieve",
            name="法律知识混合检索",
            type="RAG_RETRIEVE",
            owner="ai-platform",
            description="Milvus 混合召回 + Rerank + 引用溯源",
            version="1.0.0",
            protocol="PLATFORM",
            request_schema={
                "type": "object",
                "required": ["kb_ids", "query"],
                "properties": {
                    "kb_ids": {"type": "array", "items": {"type": "integer"}},
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
            },
            response_schema={"type": "object"},
            tags=["rag", "retrieval", "legal"],
        ),
        CapabilityCreate(
            code="ocr.document",
            name="司法材料 OCR",
            type="MODEL_VISION",
            owner="ai-platform",
            description="扫描件 OCR、版面分析与要素识别",
            version="1.0.0",
            protocol="PLATFORM",
            model_name="paddleocr",
            request_schema={"type": "object"},
            response_schema={"type": "object"},
            tags=["ocr", "vision"],
        ),
    ]


def _sample_documents() -> list[DocumentCreate]:
    return [
        DocumentCreate(
            title="中华人民共和国民法典（借款合同节选）",
            doc_type="law",
            source="seed",
            security_level="INTERNAL",
            metadata={"doc_no": "主席令第四十五号", "effective": True},
            text=(
                "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n\n"
                "借款合同对支付利息没有约定的，视为没有利息。\n\n"
                "借款合同对支付利息约定不明确，当事人不能达成补充协议的，"
                "按照当地或者当事人的交易方式、交易习惯、市场利率等因素确定利息；"
                "自然人之间借款的，视为没有利息。"
            ),
        ),
        DocumentCreate(
            title="最高人民法院关于审理民间借贷案件适用法律若干问题的规定（节选）",
            doc_type="judicial_interpretation",
            source="seed",
            security_level="INTERNAL",
            metadata={"doc_no": "法释〔2020〕17号", "effective": True},
            text=(
                "第二十五条 出借人请求借款人按照合同约定利率支付利息的，人民法院应予支持，"
                "但是双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的除外。\n\n"
                "前款所称一年期贷款市场报价利率，是指中国人民银行授权全国银行间同业拆借中心"
                "自2019年8月20日起每月发布的一年期贷款市场报价利率。"
            ),
        ),
        DocumentCreate(
            title="类案参考：某民间借贷纠纷案",
            doc_type="case",
            source="seed",
            security_level="INTERNAL",
            metadata={"cause": "民间借贷纠纷", "effective": True},
            text=(
                "裁判要旨：借贷双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的，"
                "超过部分的利息约定无效。借款人请求出借人返还已支付的超过部分利息的，"
                "人民法院应予支持。\n\n"
                "本案中，双方约定月利率3%，明显超过合同成立时一年期贷款市场报价利率四倍，"
                "法院对超过部分不予保护。"
            ),
        ),
    ]


def seed_demo() -> dict[str, Any]:
    init_db()
    settings = get_settings()
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.code == DEMO_TENANT_CODE).first()
        if tenant is None:
            tenant = Tenant(
                id=new_id(),
                code=DEMO_TENANT_CODE,
                name="演示租户",
                type="COMPANY",
                contact="demo@example.com",
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        app = db.query(App).filter(App.tenant_id == tenant.id, App.code == DEMO_APP_CODE).first()
        if app is None:
            app = App(
                id=new_id(),
                tenant_id=tenant.id,
                code=DEMO_APP_CODE,
                name="演示应用",
                type="SYSTEM",
                owner="ai-platform",
            )
            db.add(app)
            db.commit()
            db.refresh(app)

        plaintext, digest, prefix = generate_api_key()
        credential = AppCredential(
            id=new_id(),
            app_id=app.id,
            credential_type="API_KEY",
            key_hash=digest,
            key_prefix=prefix,
            created_by=0,
        )
        db.add(credential)
        db.commit()

        capability_codes: list[str] = []
        for spec in _capability_specs():
            capability = (
                db.query(Capability)
                .filter(Capability.tenant_id == 0, Capability.code == spec.code)
                .first()
            )
            if capability is None:
                capability, version = create_capability(db, spec, 0)
                version = publish_version(db, capability, version.version)
            else:
                version = (
                    db.query(CapabilityVersion)
                    .filter(
                        CapabilityVersion.capability_id == capability.id,
                        CapabilityVersion.is_default.is_(True),
                        CapabilityVersion.status == "PUBLISHED",
                    )
                    .first()
                )
                if version is None:
                    version = create_version(db, capability, spec, 0)
                    version = publish_version(db, capability, version.version)

            grant = (
                db.query(CapabilityGrant)
                .filter(
                    CapabilityGrant.app_id == app.id,
                    CapabilityGrant.capability_id == capability.id,
                    CapabilityGrant.capability_version_id.is_(None),
                    CapabilityGrant.env == "prod",
                )
                .first()
            )
            if grant is None:
                db.add(
                    CapabilityGrant(
                        id=new_id(),
                        tenant_id=tenant.id,
                        app_id=app.id,
                        capability_id=capability.id,
                        capability_version_id=None,
                        env="prod",
                        status="ACTIVE",
                        created_by=0,
                    )
                )
                db.commit()
            capability_codes.append(capability.code)

        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.code == DEMO_KB_CODE)
            .first()
        )
        if kb is None:
            kb = create_kb(
                db,
                tenant.id,
                KbCreate(
                    code=DEMO_KB_CODE,
                    name="法律知识演示库",
                    domain="legal",
                    description="民法典、民间借贷司法解释与类案参考",
                    owner="ai-platform",
                    embedding_model=settings.embedding_model,
                    chunk_config={"max_chars": 500, "overlap": 50},
                ),
            )

        existing_documents = (
            db.query(Document)
            .filter(Document.kb_id == kb.id, Document.deleted_at.is_(None))
            .count()
        )
        if existing_documents == 0:
            for document_payload in _sample_documents():
                add_document(db, kb, document_payload)

        kb_version = (
            db.query(KbVersion)
            .filter(KbVersion.kb_id == kb.id, KbVersion.version == "v1.0.0")
            .first()
        )
        if kb_version is None:
            kb_version = create_kb_version(db, kb, "v1.0.0", publish=True)

        index = (
            db.query(IndexVersion)
            .filter(IndexVersion.kb_id == kb.id, IndexVersion.status == "READY")
            .order_by(IndexVersion.created_at.desc())
            .first()
        )
        if index is None:
            index = index_kb(db, kb, "v1.0.0")
        set_alias(db, kb, "prod", "v1.0.0", index.version)

        server = (
            db.query(McpServer)
            .filter(McpServer.tenant_id == 0, McpServer.code == "demo-tools")
            .first()
        )
        if server is None:
            server = create_server(
                db,
                0,
                McpServerCreate(
                    code="demo-tools",
                    name="演示工具集",
                    transport="MOCK",
                    owner="ai-platform",
                ),
            )
        tool = (
            db.query(McpTool)
            .filter(McpTool.server_id == server.id, McpTool.name == "case.search")
            .first()
        )
        if tool is None:
            tool = create_tool(
                db,
                server,
                McpToolCreate(
                    name="case.search",
                    description="按案号查询案件摘要（演示 Mock）",
                    input_schema={
                        "type": "object",
                        "required": ["case_no"],
                        "properties": {"case_no": {"type": "string"}},
                    },
                    risk_level="LOW",
                    idempotent=True,
                ),
            )
        if (
            db.query(McpToolGrant)
            .filter(McpToolGrant.app_id == app.id, McpToolGrant.tool_id == tool.id)
            .first()
            is None
        ):
            grant_tool(db, tool, app.id)

        return {
            "tenant_id": tenant.id,
            "app_id": app.id,
            "api_key": plaintext,
            "admin_token": settings.admin_token,
            "capabilities": capability_codes,
            "kb_id": kb.id,
            "kb_version": kb_version.version,
            "index_version": index.version,
            "mcp_tool": tool.name,
        }
    finally:
        db.close()


def main() -> None:
    result = seed_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
