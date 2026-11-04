from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from judicial_ai.config import get_settings
from judicial_ai.services.decision import JevDecisionModel, RuleDecisionModel


def main() -> None:
    parser = argparse.ArgumentParser(description="JEV 影子模式离线对比")
    parser.add_argument("--input", required=True, help="JSONL，每行包含 query 字段")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.jev_base_url or not settings.jev_api_key:
        raise SystemExit("JEV_BASE_URL 与 JEV_API_KEY 未配置")
    rule = RuleDecisionModel()
    jev = JevDecisionModel(settings)
    rows = [
        json.loads(line)
        for line in Path(args.input).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    for row in rows:
        query = str(row["query"])
        started = time.perf_counter()
        rule_plan = rule.decide(query)
        jev_plan = jev.decide(query)
        results.append(
            {
                "query": query,
                "rule_intent": rule_plan.intent,
                "jev_intent": jev_plan.intent,
                "agreement": rule_plan.intent == jev_plan.intent,
                "jev_confidence": jev_plan.confidence,
                "jev_provider": jev_plan.provider,
                "fallback_from": jev_plan.fallback_from,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    agreement = sum(1 for item in results if item["agreement"]) / len(results) if results else 0
    report = {
        "queries": len(results),
        "agreement": round(agreement, 4),
        "fallback_count": sum(1 for item in results if item["fallback_from"]),
        "p50_latency_ms": round(
            statistics.median([item["latency_ms"] for item in results]), 2
        )
        if results
        else 0,
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
