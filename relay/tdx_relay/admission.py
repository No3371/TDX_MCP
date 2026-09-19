"""Admission: a tail counter and a signer, and nothing else.

Every token carries the wall clock second at which its holder may be served and
the second its admission runs out, signed by the relay.  Issuing one moves a single number forward — the tail of
the queue — so the relay keeps no record of any token, whatever the length of
the line.  Checking one is a signature check and a comparison against the
clock.

    admit_at = max(now - (burst - 1)/rate, tail)
    tail     = admit_at + 1/rate

The first term lets an idle relay hand out ``burst`` immediate admissions; the
second paces everyone else exactly 1/rate apart.

A token is good from ``admit_at`` until ``admit_at + window``.  Coming back
late is therefore the same thing as coming back with an expired token: the
holder books a new slot.  A served call renews the window, so a caller that
keeps working keeps its place and one that wanders off loses it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .tokens import Claims, TokenSigner


class State(str, Enum):
    QUEUED = "queued"
    ADMITTED = "admitted"


class QueueFull(Exception):
    """The wait would be longer than the relay is willing to promise."""


@dataclass(frozen=True)
class Status:
    state: State
    token: str
    position: int          # 0 once admitted, otherwise places left in front
    eta_seconds: float     # 0.0 once admitted
    new_token: bool = False


class Admitter:
    def __init__(
        self,
        signer: Optional[TokenSigner] = None,
        rate: float = 10.0,
        burst: float = 10.0,
        max_wait: float = 300.0,
        window: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.signer = signer or TokenSigner(clock=clock)
        self.rate = rate
        self.burst = max(burst, 1.0)
        self.max_wait = max_wait
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._tail = 0.0

    # -- issuing ---------------------------------------------------------
    def issue(self) -> Status:
        """Take the next slot in line and sign a token for it."""
        now = self._clock()
        with self._lock:
            admit_at = max(now - (self.burst - 1.0) / self.rate, self._tail)
            wait = admit_at - now
            if wait > self.max_wait:
                raise QueueFull()
            self._tail = admit_at + 1.0 / self.rate
        token = self.signer.sign(admit_at, expires_at=admit_at + self.window)
        return self._status(token, admit_at, now, new_token=True)

    def renew(self, claims: Claims) -> str:
        """Re-sign a served token with its window restarted from now.

        This is what keeps an active caller out of the queue without storing
        anything: the token is replaced on every served call, and one that
        stops being used runs out on its own.
        """
        now = self._clock()
        return self.signer.sign(now, expires_at=now + self.window)

    # -- checking ---------------------------------------------------------
    def check(self, token: Optional[str]) -> Optional[Status]:
        """Look up a token.  ``None`` means malformed, forged or expired."""
        claims = self.signer.verify(token)
        if claims is None:
            return None
        return self._status(token, claims.admit_at, self._clock())

    def claims(self, token: Optional[str]) -> Optional[Claims]:
        return self.signer.verify(token)

    def is_stale(self, token: Optional[str]) -> bool:
        """Was this one of ours, whose admission window has since closed?"""
        return self.signer.verify(token) is None and (
            self.signer.verify(token, ignore_expiry=True) is not None
        )

    def _status(self, token: str, admit_at: float, now: float, new_token: bool = False) -> Status:
        wait = admit_at - now
        if wait <= 0.0:
            return Status(State.ADMITTED, token, 0, 0.0, new_token)
        return Status(State.QUEUED, token, math.ceil(wait * self.rate), wait, new_token)

    # -- reporting ---------------------------------------------------------
    def waiting(self) -> int:
        """How many slots are booked beyond now."""
        now = self._clock()
        with self._lock:
            return max(0, math.ceil((self._tail - now) * self.rate))

    def stats(self) -> dict:
        return {
            "admit_rate_per_second": self.rate,
            "waiting": self.waiting(),
            "max_wait_seconds": self.max_wait,
            "admission_window_seconds": self.window,
        }
