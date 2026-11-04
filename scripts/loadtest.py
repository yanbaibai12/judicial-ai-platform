from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class LoadResult:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


async def one_request(
    client: httpx.AsyncClient,
    scenario: str,
    args: argparse.Namespace,
    result: LoadResult,
) -> None:
    started = time.perf_counter()
    try:
        if scenario == "health":
            response = await client.get("/healthz")
        elif scenario == "chat":
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": args.model,
                    "messages": [{"role": "user", "content": args.prompt}],
                    "max_tokens": args.max_tokens,
                },
            )
        elif scenario == "retrieve":
            response = await client.post(
                "/v1/retrieve",
                json={
                    "kb_ids": args.kb_ids,
                    "query": args.prompt,
                    "top_k": args.top_k,
                    "with_citation": True,
                    "compress": args.compress,
                },
            )
        else:
            raise ValueError(f"unknown scenario: {scenario}")
        result.status_codes[response.status_code] = (
            result.status_codes.get(response.status_code, 0) + 1
        )
        if response.status_code >= 400:
            result.errors += 1
    except Exception:  # noqa: BLE001 - load test records failures
        result.errors += 1
    finally:
        result.latencies.append((time.perf_counter() - started) * 1000)


async def worker(
    client: httpx.AsyncClient,
    scenario: str,
    args: argparse.Namespace,
    result: LoadResult,
    deadline: float,
) -> None:
    while time.time() < deadline:
        await one_request(client, scenario, args, result)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    limits = httpx.Limits(max_connections=args.concurrency * 2)
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        limits=limits,
    ) as client:
        result = LoadResult()
        deadline = time.time() + args.duration
        await asyncio.gather(
            *(
                worker(client, args.scenario, args, result, deadline)
                for _ in range(args.concurrency)
            )
        )
    total = len(result.latencies)
    return {
        "scenario": args.scenario,
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "duration_seconds": round(args.duration, 2),
        "requests": total,
        "errors": result.errors,
        "error_rate": round(result.errors / total, 6) if total else 0,
        "status_codes": result.status_codes,
        "latency_ms": {
            "p50": round(percentile(result.latencies, 0.50), 2),
            "p95": round(percentile(result.latencies, 0.95), 2),
            "p99": round(percentile(result.latencies, 0.99), 2),
            "mean": round(statistics.fmean(result.latencies), 2) if result.latencies else 0,
            "max": round(max(result.latencies), 2) if result.latencies else 0,
        },
        "qps": round(total / args.duration, 2) if args.duration else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="司法 AI 中台压测脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--scenario", choices=["health", "chat", "retrieve"], default="health")
    parser.add_argument("--model", default="llm.qwen3.chat")
    parser.add_argument("--kb-ids", nargs="+", type=int, default=[])
    parser.add_argument("--prompt", default="民间借贷利率上限如何认定？")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--compress", action="store_true")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--p95-threshold-ms", type=float, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.scenario == "retrieve" and not args.kb_ids:
        parser.error("retrieve 场景必须提供 --kb-ids")
    report = asyncio.run(run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    if args.p95_threshold_ms and report["latency_ms"]["p95"] > args.p95_threshold_ms:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
