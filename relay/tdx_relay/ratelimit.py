"""Per-key token bucket, used to limit how often one IP may request a token."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass
class _Bucket:
    credits: float
    updated: float


class RateLimiter:
    def __init__(
        self,
        burst: float = 3.0,
        rate: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 200_000,
    ) -> None:
        self.burst = max(burst, 1.0)
        self.rate = rate
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}
        self._last_sweep = clock()

    def _sweep_locked(self) -> None:
        now = self._clock()
        if now - self._last_sweep < 60.0 and len(self._buckets) < self._max_keys:
            return
        self._last_sweep = now
        # A bucket back at full credits carries no state worth keeping.
        full_after = self.burst / self.rate if self.rate > 0 else float("inf")
        stale = [k for k, b in self._buckets.items() if now - b.updated > full_after]
        for key in stale:
            del self._buckets[key]

    def take(self, key: str, cost: float = 1.0) -> Tuple[bool, float]:
        """Spend ``cost`` credits for ``key``.

        Returns ``(allowed, retry_after_seconds)``.  ``retry_after_seconds`` is
        0.0 when allowed.
        """
        with self._lock:
            self._sweep_locked()
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(credits=self.burst, updated=now)
                self._buckets[key] = bucket
            else:
                bucket.credits = min(
                    self.burst, bucket.credits + (now - bucket.updated) * self.rate
                )
                bucket.updated = now

            if bucket.credits >= cost:
                bucket.credits -= cost
                return True, 0.0
            missing = cost - bucket.credits
            retry_after = missing / self.rate if self.rate > 0 else float("inf")
            return False, retry_after

    def peek(self, key: str) -> Optional[float]:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self.burst
            now = self._clock()
            return min(self.burst, bucket.credits + (now - bucket.updated) * self.rate)
