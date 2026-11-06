"""合同风险审查：规则引擎 + 法条引用检索。

面向中小微企业的合同自查场景：
1. 条款切分（按段落/条号边界）；
2. 规则匹配（利率、违约金、免责、管辖、单方解除、连带责任等）；
3. 必备条款完整性检查；
4. 命中风险后从指定知识库检索关联法条/类案，附引用溯源。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError
from ..security import Principal
from ..text_utils import bound_text
from .retrieval import retrieve

logger = logging.getLogger("judicial_ai.contract_review")
RULESET_VERSION = "example-rules-v2"

# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------

_CLAUSE_SPLIT = re.compile(
    r"\n\s*\n|(?:(?<=^)|(?<=[。\n]))第[一二三四五六七八九十百千零〇0-9]+条"
)


def split_clauses(text: str) -> list[str]:
    """按段落与「第X条」切分条款；扫描前套长度门限以限定正则耗时。"""
    text, _ = bound_text(text)
    raw = re.split(r"\n\s*\n", text)
    clauses: list[str] = []
    for block in raw:
        block = block.strip()
        if not block:
            continue
        # 段内再按 "第X条" 切，但保留条号前缀
        parts = re.split(r"(?=第[一二三四五六七八九十百千零〇0-9]+条)", block)
        clauses.extend(part.strip() for part in parts if part.strip())
    return clauses or [text.strip()]


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

@dataclass
class RiskRule:
    rule_id: str
    name: str
    severity: str  # high | medium | low
    pattern: re.Pattern[str]
    description: str
    suggestion: str
    legal_basis: str
RULES: list[RiskRule] = [
    RiskRule(
        rule_id="INTEREST_CAP",
        name="借款利率条款待核查",
        severity="high",
        pattern=re.compile(r"年利率[^。；\n]{0,20}?(\d+(?:\.\d+)?)\s*%|利滚利|复利计息"),
        description="检出利率或复利相关表述，不能仅凭数值判断是否超过适用限制。",
        suggestion="请专业人员结合合同类型、成立时间、计息方式和适用规则核查。",
        legal_basis="《民法典》第六百八十条（示例线索，适用性和现行文本需核验）。",
    ),
    RiskRule(
        rule_id="PENALTY_EXCESSIVE",
        name="违约金条款待核查",
        severity="medium",
        pattern=re.compile(r"[^。；\n]{0,40}违约金[^。；\n]{0,40}"),
        description="检出违约金相关表述，比例本身不足以判断约定是否适当。",
        suggestion="请专业人员结合实际损失、合同性质和相关事实核查。",
        legal_basis="《民法典》第五百八十五条（示例线索，适用性和现行文本需核验）。",
    ),
    RiskRule(
        rule_id="UNFAIR_EXEMPTION",
        name="不公平免责条款",
        severity="high",
        pattern=re.compile(r"免除[^。；\n]{0,15}责任|不承担[^。；\n]{0,10}(赔偿|违约)?责任|最终解释权"),
        description="检出免责或解释权相关表述，是否构成不公平条款需要结合全文判断。",
        suggestion="请专业人员核查责任分配、条款性质及提示情况。",
        legal_basis="《民法典》第四百九十七条（示例线索，适用性和现行文本需核验）。",
    ),
    RiskRule(
        rule_id="UNFAIR_JURISDICTION",
        name="管辖条款不利",
        severity="medium",
        pattern=re.compile(r"管辖[^。；\n]{0,25}(原告|甲方|乙方|对方|出借人)所在地"),
        description="检出所在地管辖相关表述，实际影响取决于当事人身份及争议情况。",
        suggestion="请专业人员核查当事人身份、争议联系及约定的适用性。",
        legal_basis="《民事诉讼法》相关规定（示例线索，具体条文及适用性需核验）。",
    ),
    RiskRule(
        rule_id="UNILATERAL_TERMINATION",
        name="单方解除权失衡",
        severity="medium",
        pattern=re.compile(r"(有权|可)[^。；\n]{0,10}单方(面)?解除|无需[^。；\n]{0,8}(通知|同意)[^。；\n]{0,8}解除"),
        description="检出单方解除相关表述，权利归属和适用条件需结合全文核查。",
        suggestion="请专业人员核查解除条件、通知期限及解除后的结算方式。",
        legal_basis="《民法典》第五百六十三条（示例线索，适用性和现行文本需核验）。",
    ),
    RiskRule(
        rule_id="UNLIMITED_LIABILITY",
        name="无限连带责任",
        severity="high",
        pattern=re.compile(r"无限连带|连带责任[^。；\n]{0,15}(全部|所有)"),
        description="检出连带责任相关表述，责任主体、范围和后果需要结合全文判断。",
        suggestion="请专业人员核查责任主体、责任范围和具体金额。",
        legal_basis="《民法典》第一百七十八条（示例线索，适用性和现行文本需核验）。",
    ),
    RiskRule(
        rule_id="AUTOMATIC_RENEWAL",
        name="自动续期/自动扣款",
        severity="low",
        pattern=re.compile(r"自动续(期|约)|自动扣[款费]|到期[^。；\n]{0,10}自动(续|展)"),
        description="检出自动续期或自动扣款表述，具体条件需要结合全文判断。",
        suggestion="请专业人员核查续期、扣款及通知条件。",
        legal_basis="《民法典》相关规定（示例线索，具体条文及适用性需核验）。",
    ),
]

# 必备条款关键词 -> 缺失时的提示
REQUIRED_CLAUSES: list[dict[str, str]] = [
    {"key": "争议解决|仲裁|诉讼|管辖", "name": "争议解决条款", "severity": "medium",
     "suggestion": "补充争议解决方式（诉讼管辖或仲裁机构）与适用法律。",
     "legal_basis": "《民事诉讼法》相关规定（示例线索，具体条文及适用性需核验）。"},
    {"key": "违约责任|违约金|赔偿", "name": "违约责任条款", "severity": "medium",
     "suggestion": "补充违约情形、违约金或损失赔偿的计算方式。",
     "legal_basis": "《民法典》第五百七十七条（示例线索，适用性和现行文本需核验）。"},
    {"key": "付款|支付|价款|报酬", "name": "价款与支付条款", "severity": "low",
     "suggestion": "明确金额、支付方式、支付节点与发票开具。",
     "legal_basis": "《民法典》第五百一十条（示例线索，适用性和现行文本需核验）。"},
    {"key": "期限|日期|交付|履行", "name": "履行期限条款", "severity": "low",
     "suggestion": "明确各主要义务的履行期限与逾期处理。",
     "legal_basis": "《民法典》相关规定（示例线索，具体条文及适用性需核验）。"},
]


# ---------------------------------------------------------------------------
# 审查主流程
# ---------------------------------------------------------------------------

def _clause_query(clause: str, rule_name: str) -> str:
    excerpt = clause[:80].replace("\n", " ")
    return f"{rule_name} {excerpt}"


def review_contract(
    db: Session,
    tenant_id: int,
    text: str,
    kb_ids: list[int] | None = None,
    top_k: int = 3,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """审查合同文本，返回风险清单 + 必备条款缺失 + 法条引用。"""
    settings = get_settings()
    if settings.is_production or not settings.contract_review_enabled:
        raise ApiError("FEATURE_DISABLED", "示例合同审查未开放，请使用经审核的业务流程", 503)
    started = time.perf_counter()
    if not text or not text.strip():
        raise ApiError("INVALID_ARGUMENT", "合同文本不能为空", 400)

    clauses = split_clauses(text)
    findings: list[dict[str, Any]] = []

    # 1) 风险规则匹配
    for clause in clauses:
        for rule in RULES:
            for match in rule.pattern.finditer(clause):
                findings.append(
                    {
                        "rule_id": rule.rule_id,
                        "rule_name": rule.name,
                        "severity": rule.severity,
                        "clause_excerpt": clause[:200],
                        "matched_text": match.group(0)[:80],
                        "description": rule.description,
                        "suggestion": rule.suggestion,
                        "legal_basis": rule.legal_basis,
                        "legal_basis_status": "UNVERIFIED",
                        "citations": [],
                        "citation_status": "NOT_REQUESTED" if not kb_ids else "NOT_FOUND",
                        "requires_human_review": True,
                    }
                )
                break  # 同一条款同一规则只报一次

    # 2) 必备条款完整性
    full_text = text
    for required in REQUIRED_CLAUSES:
        if not re.search(required["key"], full_text):
            findings.append(
                {
                    "rule_id": f"MISSING_{required['name'][:4]}",
                    "rule_name": f"缺少{required['name']}",
                    "severity": required["severity"],
                    "clause_excerpt": "",
                    "matched_text": "",
                    "description": (
                        f"未检出{required['name']}相关关键词，不代表合同确实缺少该条款。"
                    ),
                    "suggestion": required["suggestion"],
                    "legal_basis": required["legal_basis"],
                    "legal_basis_status": "UNVERIFIED",
                    "citations": [],
                    "citation_status": "NOT_REQUESTED" if not kb_ids else "NOT_FOUND",
                    "requires_human_review": True,
                }
            )

    # 3) 法条引用检索（失败不影响审查结果）
    if kb_ids:
        for finding in findings:
            if not finding["clause_excerpt"] and not finding["rule_name"]:
                continue
            try:
                result = retrieve(
                    db,
                    tenant_id=tenant_id,
                    kb_ids=kb_ids,
                    query=_clause_query(finding["clause_excerpt"], finding["rule_name"]),
                    top_k=top_k,
                    with_citation=True,
                    principal=principal,
                )
                finding["citations"] = [
                    {
                        "chunk_id": item.get("chunk_id"),
                        "document_id": item.get("doc_id"),
                        "title": (item.get("citation") or {}).get("title"),
                        "snippet": (item.get("text") or "")[:120],
                        "score": item.get("score"),
                        "source_url": (item.get("citation") or {}).get("source_url"),
                    }
                    for item in (result.get("results") or [])[:top_k]
                ]
                if finding["citations"]:
                    finding["citation_status"] = "RETRIEVED_UNVERIFIED"
            except ApiError as exc:
                if exc.status_code in {401, 403}:
                    raise
                finding["citation_status"] = "UNAVAILABLE"
                logger.warning("contract_citation_retrieval_unavailable code=%s", exc.code)
            except Exception:  # noqa: BLE001 - 引用检索降级不阻塞审查
                finding["citation_status"] = "UNAVAILABLE"
                logger.warning("contract_citation_retrieval_unavailable")

    # 4) 汇总
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    if counts["high"] > 0:
        overall = "HIGH"
    elif counts["medium"] > 0:
        overall = "MEDIUM"
    elif counts["low"] > 0:
        overall = "LOW"
    else:
        overall = "REVIEW_REQUIRED"

    return {
        "clause_count": len(clauses),
        "finding_count": len(findings),
        "severity_counts": counts,
        "overall_risk": overall,
        "review_status": "HUMAN_REVIEW_REQUIRED",
        "ruleset_version": RULESET_VERSION,
        "findings": findings,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "disclaimer": (
            "本审查为 AI 辅助示例筛查，规则和法条线索未经专业审核，"
            "检索引用未经适用性核验；未检出不代表无风险，不构成法律意见，请由专业人员复核。"
        ),
    }


def get_risk_rules() -> list[dict[str, str]]:
    """暴露当前规则清单（供前端展示/测试）。"""
    settings = get_settings()
    return [
        {
            "rule_id": rule.rule_id,
            "name": rule.name,
            "severity": rule.severity,
            "description": rule.description,
            "legal_basis": rule.legal_basis,
            "engine": RULESET_VERSION,
            "legal_basis_status": "UNVERIFIED",
            "segment_aware": str(settings.rag_compression_segment_aware),
        }
        for rule in RULES
    ]
