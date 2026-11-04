"""In-memory token-bucket rate limiter."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Allows ``per_minute`` requests per key, refilled continuously."""

    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._now = now
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + max(0.0, now - last) * self._rate)
            allowed = tokens >= 1.0
            self._buckets[key] = (tokens - 1.0 if allowed else tokens, now)
            return allowed
