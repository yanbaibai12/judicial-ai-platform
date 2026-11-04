"""咨询报告生成：把合同审查结论汇编为结构化 Markdown/HTML 报告。"""

from __future__ import annotations

import html as html_module
from datetime import UTC, datetime
from typing import Any

SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}
RISK_LABEL = {
    "HIGH": "高优先级线索，待复核",
    "MEDIUM": "中优先级线索，待复核",
    "LOW": "低优先级线索，待复核",
    "PASS": "未检出线索，仍需复核",
    "REVIEW_REQUIRED": "未检出线索，仍需复核",
}
CITATION_LABEL = {
    "NOT_REQUESTED": "未请求知识库引用",
    "NOT_FOUND": "未检索到引用",
    "UNAVAILABLE": "引用检索不可用",
    "RETRIEVED_UNVERIFIED": "已检索到相关片段，未经法律适用性核验",
}


def _fmt_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _citations_md(citations: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, citation in enumerate(citations, 1):
        title = citation.get("title") or "知识库片段"
        snippet = (citation.get("snippet") or "").replace("\n", " ")
        score = citation.get("score")
        suffix = f"（相关度 {score}）" if score is not None else ""
        lines.append(f"  {index}. {title}{suffix}：{snippet}")
    return lines


def render_markdown(review: dict[str, Any], title: str) -> str:
    risk = str(review.get("overall_risk") or "")
    lines = [
        "# 合同风险审查报告",
        "",
        "**示例筛查，必须由专业人员复核；法条线索及检索片段未经核验。**",
        "",
        f"- **合同名称**：{title}",
        f"- **生成时间**：{_fmt_ts()}",
        f"- **综合风险**：**{RISK_LABEL.get(risk, risk)}**"
        f"（高 {review['severity_counts'].get('high', 0)} /"
        f" 中 {review['severity_counts'].get('medium', 0)} /"
        f" 低 {review['severity_counts'].get('low', 0)}，"
        f"共 {review.get('finding_count', 0)} 项发现，"
        f"覆盖 {review.get('clause_count', 0)} 个条款）",
        "",
    ]

    order = {"high": 0, "medium": 1, "low": 2, "missing": 3}
    findings = sorted(
        review.get("findings") or [],
        key=lambda item: (
            order.get(item.get("severity", "low"), 3),
            item.get("rule_id", ""),
        ),
    )

    if not findings:
        lines.append("## 结论\n\n未检出规则线索不代表无风险，请由专业人员复核。\n")
    else:
        for number, finding in enumerate(findings, 1):
            severity = finding.get("severity", "low")
            lines.append(
                f"## {number}. [{SEVERITY_LABEL.get(severity, severity)}] "
                f"{finding.get('rule_name', '')}"
            )
            excerpt = finding.get("clause_excerpt") or ""
            matched = finding.get("matched_text") or ""
            if excerpt or matched:
                lines.append(f"- **涉及条款**：{excerpt or matched}")
            if finding.get("description"):
                lines.append(f"- **风险说明**：{finding['description']}")
            if finding.get("suggestion"):
                lines.append(f"- **修改建议**：{finding['suggestion']}")
            if finding.get("legal_basis"):
                lines.append(f"- **待核验法条线索**：{finding['legal_basis']}")
            status = finding.get("citation_status")
            if status in CITATION_LABEL:
                lines.append(f"- **引用状态**：{CITATION_LABEL[status]}")
            citations = _citations_md(finding.get("citations") or [])
            if citations:
                lines.append("- **待核验知识库片段**：")
                lines.extend(citations)
            lines.append("")

    lines.append("---")
    lines.append(f"*{review.get('disclaimer', '本报告为 AI 辅助筛查，不构成法律意见。')}*")
    return "\n".join(lines)


def _esc(text: Any) -> str:
    return html_module.escape(str(text or ""))


def render_html(review: dict[str, Any], title: str) -> str:
    findings = sorted(
        review.get("findings") or [],
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(item.get("severity", "low"), 3),
            item.get("rule_id", ""),
        ),
    )
    blocks: list[str] = []
    for number, finding in enumerate(findings, 1):
        severity = finding.get("severity", "low")
        citations = "".join(
            f"<li><b>{_esc(c.get('title') or '知识库片段')}</b>："
            f"{_esc(c.get('snippet') or '')}</li>"
            for c in finding.get("citations") or []
        )
        blocks.append(
            "<section class='finding {cls}'>"
            "<h3>{no}. [{label}] {name}</h3>"
            "<ul>"
            "<li><b>涉及条款</b>：{clause}</li>"
            "<li><b>风险说明</b>：{desc}</li>"
            "<li><b>修改建议</b>：{sugg}</li>"
            "<li><b>待核验法条线索</b>：{basis}</li>"
            "<li><b>引用状态</b>：{citation_status}</li>"
            "{cite_block}"
            "</ul></section>".format(
                cls=severity,
                no=number,
                label=SEVERITY_LABEL.get(severity, severity),
                name=_esc(finding.get("rule_name")),
                clause=_esc(
                    finding.get("clause_excerpt") or finding.get("matched_text") or "（缺失条款）"
                ),
                desc=_esc(finding.get("description")),
                sugg=_esc(finding.get("suggestion")),
                basis=_esc(finding.get("legal_basis")),
            citation_status=_esc(CITATION_LABEL.get(finding.get("citation_status"), "待核验")),
            cite_block=(
                f"<li><b>待核验知识库片段</b>：<ul>{citations}</ul></li>" if citations else ""
            ),
            )
        )

    body = "\n".join(blocks) or "<p>未检出规则线索不代表无风险，请由专业人员复核。</p>"
    counts = review.get("severity_counts") or {}
    risk = str(review.get("overall_risk") or "")
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>合同风险审查报告 - {title}</title>"
        "<style>"
        "body{{font-family:'Microsoft YaHei',sans-serif;max-width:900px;"
        "margin:24px auto;padding:0 16px;color:#222}}"
        ".finding{{border-left:4px solid #ccc;padding:8px 16px;margin:16px 0;background:#fafafa}}"
        ".finding.high{{border-color:#d33;background:#fdf2f2}}"
        ".finding.medium{{border-color:#e6a23c;background:#fdf8ef}}"
        ".finding.low{{border-color:#67c23a;background:#f4faf0}}"
        "h3{{margin:4px 0}}ul{{margin:4px 0;padding-left:20px}}li{{margin:4px 0}}"
        ".meta{{color:#666;font-size:14px}}"
        "</style></head><body>"
        "<h1>合同风险审查报告</h1>"
        "<p><b>示例筛查，必须由专业人员复核；法条线索及检索片段未经核验。</b></p>"
        "<p class='meta'>合同名称：{t}｜生成时间：{ts}｜综合风险：<b>{risk}</b>"
        "（高 {h} / 中 {m} / 低 {l}，共 {total} 项发现，覆盖 {clauses} 个条款）</p>"
        "{body}"
        "<hr><p class='meta'>{disclaimer}</p>"
        "</body></html>".format(
            title=_esc(title),
            t=_esc(title),
            ts=_esc(_fmt_ts()),
            risk=_esc(RISK_LABEL.get(risk, risk)),
            h=counts.get("high", 0),
            m=counts.get("medium", 0),
            l=counts.get("low", 0),
            total=review.get("finding_count", 0),
            clauses=review.get("clause_count", 0),
            body=body,
            disclaimer=_esc(review.get("disclaimer", "本报告为 AI 辅助筛查，不构成法律意见。")),
        )
    )


def generate_report(review: dict[str, Any], title: str, fmt: str = "markdown") -> dict[str, Any]:
    if fmt not in {"markdown", "md", "html"}:
        raise ValueError(f"不支持的报告格式: {fmt}")
    content = (
        render_markdown(review, title)
        if fmt in {"markdown", "md"}
        else render_html(review, title)
    )
    suffix = "html" if fmt == "html" else "md"
    return {
        "format": suffix,
        "title": f"{title}-合同风险审查报告",
        "content": content,
        "overall_risk": review.get("overall_risk"),
        "review_status": review.get("review_status"),
        "ruleset_version": review.get("ruleset_version"),
        "finding_count": review.get("finding_count", 0),
    }
