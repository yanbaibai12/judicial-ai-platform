from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any

from ..config import get_settings
from ..errors import ApiError, sanitize_exception_summary


class MemoryCache:
    def __init__(self, max_items: int = 2048) -> None:
        self._max_items = max_items
        self._items: OrderedDict[str, tuple[float | None, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at is not None and expires_at <= time.time():
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        with self._lock:
            self._items[key] = (expires_at, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max_items:
                self._items.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def incr(self, key: str, ttl_seconds: int) -> int:
        with self._lock:
            now = time.time()
            expires_at, value = self._items.get(key, (None, "0"))
            if expires_at is not None and expires_at <= now:
                expires_at = None
                value = "0"
            count = int(value) + 1
            self._items[key] = (
                expires_at or (now + ttl_seconds),
                str(count),
            )
            self._items.move_to_end(key)
            return count

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": "memory", "items": len(self._items)}


class RedisCache:
    def __init__(self, client: Any, prefix: str) -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:cache:{key}"

    def get(self, key: str) -> str | None:
        try:
            return self._client.get(self._key(key))
        except Exception:  # noqa: BLE001 - cache must not break requests
            return None

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        try:
            if ttl_seconds:
                self._client.setex(self._key(key), ttl_seconds, value)
            else:
                self._client.set(self._key(key), value)
        except Exception:  # noqa: BLE001
            return

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._key(key))
        except Exception:  # noqa: BLE001
            return

    def incr(self, key: str, ttl_seconds: int) -> int:
        try:
            redis_key = f"{self._prefix}:counter:{key}"
            value = int(self._client.incr(redis_key))
            if value == 1:
                self._client.expire(redis_key, ttl_seconds)
            return value
        except Exception:  # noqa: BLE001
            return 0

    def health(self) -> dict[str, Any]:
        try:
            self._client.ping()
            return {"status": "ok", "backend": "redis"}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "backend": "redis",
                "error": sanitize_exception_summary(exc),
            }


class Cache:
    def __init__(self, backend: RedisCache | MemoryCache, fail_open: bool) -> None:
        self.backend = backend
        self.fail_open = fail_open

    def get(self, key: str) -> str | None:
        return self.backend.get(key)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.backend.set(key, value, ttl_seconds)

    def delete(self, key: str) -> None:
        self.backend.delete(key)

    def get_json(self, key: str) -> Any:
        raw = self.backend.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self.backend.set(key, json.dumps(value, ensure_ascii=False), ttl_seconds)

    def incr(self, key: str, ttl_seconds: int) -> int:
        return self.backend.incr(key, ttl_seconds)

    def health(self) -> dict[str, Any]:
        return self.backend.health()


@lru_cache
def get_cache() -> Cache:
    settings = get_settings()
    if settings.redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=settings.redis_socket_timeout_seconds,
                socket_connect_timeout=settings.redis_socket_timeout_seconds,
            )
            client.ping()
            return Cache(
                RedisCache(client, settings.redis_key_prefix),
                settings.rate_limit_fail_open,
            )
        except Exception as exc:  # noqa: BLE001
            if not settings.rate_limit_fail_open:
                raise ApiError(
                    "DEPENDENCY_UNAVAILABLE",
                    "Redis 不可用，生产策略禁止降级",
                    503,
                    {"dependency": "redis", "error": str(exc)[:240]},
                ) from exc
    return Cache(MemoryCache(), settings.rate_limit_fail_open)
