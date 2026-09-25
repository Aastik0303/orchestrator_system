"""Cache and rate-limit backends.

In-memory implementations are the default and are correct for a single
process. When `REDIS_URL` is configured, Redis-backed implementations are used
so limits and cached values are shared by every API replica and worker. Redis
is only used for these ephemeral concerns; durable state lives in the database.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from functools import lru_cache
from typing import Any, Protocol

from app.config import get_settings

logger = logging.getLogger("orchestrator.cache")


class Cache(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class RateLimiter(Protocol):
    def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Record one hit. Returns (allowed, retry_after_seconds)."""
        ...


class MemoryCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)


class MemoryRateLimiter:
    """Sliding-window limiter (per process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and window[0] <= now - window_seconds:
                window.popleft()
            if len(window) >= limit:
                retry_after = max(1, int(window[0] + window_seconds - now) + 1)
                return False, retry_after
            window.append(now)
            if len(self._hits) > 10_000:
                # Drop idle keys so the limiter cannot grow without bound.
                for idle_key in [k for k, v in self._hits.items() if not v][:5_000]:
                    self._hits.pop(idle_key, None)
            return True, 0


class RedisCache:
    def __init__(self, client: Any, prefix: str = "orch:cache:") -> None:
        self._client = client
        self._prefix = prefix

    def get(self, key: str) -> str | None:
        try:
            value = self._client.get(self._prefix + key)
        except Exception as exc:  # cache failures must never fail a request
            logger.warning("redis_cache_get_failed", extra={"error_type": exc.__class__.__name__})
            return None
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            self._client.set(self._prefix + key, value, ex=max(1, ttl_seconds))
        except Exception as exc:
            logger.warning("redis_cache_set_failed", extra={"error_type": exc.__class__.__name__})


class RedisRateLimiter:
    """Fixed-window counter shared across replicas (INCR + EXPIRE)."""

    def __init__(self, client: Any, prefix: str = "orch:rl:") -> None:
        self._client = client
        self._prefix = prefix

    def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        bucket = int(time.time() // window_seconds)
        redis_key = f"{self._prefix}{key}:{bucket}"
        try:
            pipeline = self._client.pipeline()
            pipeline.incr(redis_key)
            pipeline.expire(redis_key, window_seconds + 1)
            count = int(pipeline.execute()[0])
        except Exception as exc:
            # Fail open on limiter outages but make the degradation visible.
            logger.warning("redis_rate_limit_failed", extra={"error_type": exc.__class__.__name__})
            return True, 0
        if count > limit:
            retry_after = window_seconds - int(time.time() % window_seconds)
            return False, max(1, retry_after)
        return True, 0


def _redis_client() -> Any | None:
    url = get_settings().redis_url
    if not url:
        return None
    try:
        import redis
    except ImportError:
        logger.warning("redis_package_missing")
        return None
    return redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)


@lru_cache
def get_cache() -> Cache:
    client = _redis_client()
    if client is not None:
        return RedisCache(client)
    return MemoryCache(get_settings().llm_cache_size)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    client = _redis_client()
    if client is not None:
        return RedisRateLimiter(client)
    return MemoryRateLimiter()


def reset_backends() -> None:
    get_cache.cache_clear()
    get_rate_limiter.cache_clear()
