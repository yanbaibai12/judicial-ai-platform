from __future__ import annotations

import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache

from ..config import get_settings
from ..errors import ApiError
from ..metrics import record_rate_limited


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after_ms: int
    backend: str

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(max(1, math.ceil(self.reset_after_ms / 1000))),
        }


class RateLimiter:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._local: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._redis = None
        if self._settings.redis_url:
            try:
                import redis

                client = redis.Redis.from_url(
                    self._settings.redis_url,
                    decode_responses=True,
                    socket_timeout=self._settings.redis_socket_timeout_seconds,
                    socket_connect_timeout=self._settings.redis_socket_timeout_seconds,
                )
                client.ping()
                self._redis = client
            except Exception as exc:  # noqa: BLE001
                if not self._settings.rate_limit_fail_open:
                    raise ApiError(
                        "DEPENDENCY_UNAVAILABLE",
                        "限流依赖 Redis 不可用",
                        503,
                        {"dependency": "redis", "error": str(exc)[:240]},
                    ) from exc
                self._redis = None

    @property
    def window_seconds(self) -> float:
        return max(0.1, self._settings.rate_limit_window_seconds)

    def consume(
        self, key: str, limit: int | None, window_seconds: float | None = None
    ) -> RateLimitResult:
        window = window_seconds or self.window_seconds
        if not self._settings.rate_limit_enabled or not limit or limit <= 0:
            return RateLimitResult(True, limit or 0, limit or 0, 0, "disabled")

        if self._redis is not None:
            try:
                now = time.time()
                redis_key = f"{self._settings.redis_key_prefix}:rl:{key}:{window:g}"
                pipe = self._redis.pipeline()
                pipe.zremrangebyscore(redis_key, 0, now - window)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {f"{now}:{uuid.uuid4().hex[:6]}": now})
                pipe.expire(redis_key, max(1, int(window) + 2))
                _, count, _, _ = pipe.execute()
                allowed = int(count) < limit
                remaining = max(0, limit - int(count) - (1 if allowed else 0))
                if not allowed:
                    record_rate_limited("redis")
                return RateLimitResult(
                    allowed,
                    limit,
                    remaining,
                    int(window * 1000),
                    "redis",
                )
            except Exception as exc:  # noqa: BLE001
                if not self._settings.rate_limit_fail_open:
                    raise ApiError(
                        "DEPENDENCY_UNAVAILABLE",
                        "限流依赖 Redis 不可用",
                        503,
                        {"dependency": "redis", "error": str(exc)[:240]},
                    ) from exc

        with self._lock:
            bucket = self._local[key]
            now = time.time()
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            allowed = len(bucket) < limit
            if allowed:
                bucket.append(now)
            remaining = max(0, limit - len(bucket))
            if not allowed:
                record_rate_limited("local")
            return RateLimitResult(
                allowed,
                limit,
                remaining,
                int(window * 1000),
                "local",
            )

    def allow(self, key: str, limit: int | None, window_seconds: float | None = None) -> bool:
        return self.consume(key, limit, window_seconds).allowed

    def check(self, key: str, limit: int | None, window_seconds: float | None = None) -> None:
        result = self.consume(key, limit, window_seconds)
        if not result.allowed:
            raise ApiError(
                "RATE_LIMITED",
                "请求过于频繁，请稍后重试",
                429,
                {
                    "retry_after_ms": result.reset_after_ms,
                    "limit": result.limit,
                    "remaining": max(0, result.remaining),
                },
            )


@lru_cache
def get_rate_limiter() -> RateLimiter:
    return RateLimiter()
