from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Iterable

HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_text(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items()))
    return "{" + rendered + "}"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], list[float]
        ] = defaultdict(list)

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def increment(
        self, name: str, labels: dict[str, str] | None = None, value: float = 1.0
    ) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def add_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = self._gauges.get(key, 0.0) + value

    def observe(
        self, name: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        if not math.isfinite(value) or value < 0:
            return
        key = self._key(name, labels)
        with self._lock:
            self._histograms[key].append(value)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {key: list(values) for key, values in self._histograms.items()}

        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{_labels_text(dict(labels))} {value:.6g}")

        for (name, labels), value in sorted(gauges.items()):
            lines.append(f"{name}{_labels_text(dict(labels))} {value:.6g}")

        for (name, labels), values in sorted(histograms.items()):
            label_dict = dict(labels)
            for bucket in HISTOGRAM_BUCKETS:
                count = sum(1 for item in values if item <= bucket)
                bucket_labels = {**label_dict, "le": str(bucket)}
                lines.append(f"{name}_bucket{_labels_text(bucket_labels)} {count}")
            lines.append(
                f"{name}_bucket{_labels_text({**label_dict, 'le': '+Inf'})} {len(values)}"
            )
            lines.append(f"{name}_sum{_labels_text(label_dict)} {sum(values):.6f}")
            lines.append(f"{name}_count{_labels_text(label_dict)} {len(values)}")

        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


metrics = MetricsRegistry()


def record_http_request(method: str, path: str, status: int, duration_seconds: float) -> None:
    labels = {"method": method.upper(), "path": path}
    metrics.increment("judicial_http_requests_total", {**labels, "status": str(status)})
    metrics.observe("judicial_http_request_duration_seconds", duration_seconds, labels)


def record_auth_failure(reason: str) -> None:
    metrics.increment("judicial_auth_failures_total", {"reason": reason})


def record_rate_limited(scope: str) -> None:
    metrics.increment("judicial_rate_limited_total", {"scope": scope})


def record_job_event(job_type: str, status: str) -> None:
    metrics.increment("judicial_jobs_total", {"type": job_type, "status": status})


def record_job_duration(job_type: str, duration_seconds: float) -> None:
    metrics.observe("judicial_job_duration_seconds", duration_seconds, {"type": job_type})


def record_capability_invocation(capability_type: str, status: str) -> None:
    metrics.increment(
        "judicial_capability_invocations_total",
        {"type": capability_type, "status": status},
    )


def record_citation_integrity(status: str) -> None:
    """统计检索引用的可信度分布（VERIFIED / DEGRADED / INVALID / NONE）。"""

    metrics.increment("judicial_citation_integrity_total", {"status": status})


def record_document_parse(parser: str, status: str) -> None:
    """统计文档解析结果（PARSED / PARTIAL / FAILED），用于发现解析质量退化。"""

    metrics.increment("judicial_document_parse_total", {"parser": parser, "status": status})


def render_metrics() -> str:
    return metrics.render()


def reset_metrics() -> None:
    metrics.reset()


def iter_metric_names() -> Iterable[str]:
    with metrics._lock:  # noqa: SLF001 - diagnostics helper
        return sorted({name for name, _ in metrics._counters})
