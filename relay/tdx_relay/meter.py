"""Moving average of the request rate.

An exponentially weighted estimator: every request decays the running value by
``exp(-elapsed / window)`` and then adds ``1 / window``, so the value settles at
the arrival rate in requests per second.  Old traffic fades instead of falling
off a cliff, and nothing but two floats is kept.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable


class RateMeter:
    def __init__(self, window: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._rate = 0.0
        self._updated = clock()

    def _decay_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        if elapsed > 0.0:
            self._rate *= math.exp(-elapsed / self.window)

    def record(self, count: int = 1) -> float:
        """Count requests and return the rate including them."""
        with self._lock:
            self._decay_locked()
            self._rate += count / self.window
            return self._rate

    def value(self) -> float:
        with self._lock:
            self._decay_locked()
            return self._rate
