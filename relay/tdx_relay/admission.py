"""FIFO admission queue.

A caller that has no valid token gets one minted and put at the back of the
queue.  The queue releases at most ``rate`` tokens per second, in issue order.
A token is admitted as soon as the queue has drained past it; from then on the
same token serves data until it goes idle for ``admitted_ttl`` seconds.

Admission is computed lazily instead of by a background task: every token
carries the sequence number it was issued with, and a token is admitted when
its sequence number is below the queue's drain watermark.  That makes both the
position lookup and the admission itself O(1), and makes the whole thing
testable with an injected clock.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional


class State(str, Enum):
    QUEUED = "queued"
    ADMITTED = "admitted"


@dataclass
class Ticket:
    token: str
    seq: int
    issued_at: float
    last_seen: float
    ip: str


@dataclass(frozen=True)
class Status:
    state: State
    token: str
    position: int          # 0 once admitted, otherwise places left in front
    eta_seconds: float     # 0.0 once admitted
    new_token: bool = False


class QueueFull(Exception):
    """Raised when the waiting line is longer than the configured limit."""


class AdmissionQueue:
    def __init__(
        self,
        rate: float = 10.0,
        burst: Optional[float] = None,
        admitted_ttl: float = 3600.0,
        queued_ttl: float = 900.0,
        queue_limit: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = rate if burst is None else max(burst, 1.0)
        self.admitted_ttl = admitted_ttl
        self.queued_ttl = queued_ttl
        self.queue_limit = queue_limit
        self._clock = clock

        self._lock = threading.Lock()
        self._tickets: Dict[str, Ticket] = {}
        self._issued = 0        # sequence numbers handed out so far
        self._admitted = 0      # drain watermark: seq < _admitted is admitted
        self._credits = self.burst
        self._was_empty = True
        self._last_drain = clock()
        self._last_sweep = clock()

    # -- internals ------------------------------------------------------
    def _drain(self) -> None:
        """Move the watermark forward by whatever the rate has earned."""
        now = self._clock()
        elapsed = max(0.0, now - self._last_drain)
        self._last_drain = now
        self._credits += elapsed * self.rate
        if self._was_empty:
            # Idle time may not bank more than one burst: a queue nobody is
            # standing in does not earn admissions for a future crowd.
            self._credits = min(self._credits, self.burst)

        waiting = self._issued - self._admitted
        grant = min(int(self._credits), waiting)
        if grant > 0:
            self._admitted += grant
            self._credits -= grant
        self._was_empty = self._issued == self._admitted

    def _expired_locked(self, ticket: Ticket) -> bool:
        now = self._clock()
        if ticket.seq < self._admitted:
            return now - ticket.last_seen > self.admitted_ttl
        return now - ticket.issued_at > self.queued_ttl

    def _sweep_locked(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_sweep < 30.0:
            return
        self._last_sweep = now
        stale = [token for token, t in self._tickets.items() if self._expired_locked(t)]
        for token in stale:
            del self._tickets[token]

    def _status_locked(self, ticket: Ticket) -> Status:
        if ticket.seq < self._admitted:
            return Status(State.ADMITTED, ticket.token, 0, 0.0)
        position = ticket.seq - self._admitted + 1
        return Status(State.QUEUED, ticket.token, position, position / self.rate)

    # -- public API -----------------------------------------------------
    def issue(self, ip: str = "") -> Status:
        """Mint a token and put it at the back of the queue."""
        with self._lock:
            self._drain()
            self._sweep_locked()
            if self._issued - self._admitted >= self.queue_limit:
                raise QueueFull()
            now = self._clock()
            token = secrets.token_urlsafe(24)
            ticket = Ticket(token=token, seq=self._issued, issued_at=now, last_seen=now, ip=ip)
            self._issued += 1
            self._tickets[token] = ticket
            # Drain again so a caller arriving to an idle relay is served at
            # once instead of being told to wait for a queue of one.
            self._drain()
            status = self._status_locked(ticket)
            return Status(status.state, status.token, status.position, status.eta_seconds, True)

    def check(self, token: Optional[str]) -> Optional[Status]:
        """Look up a token.  ``None`` means unknown, expired or malformed."""
        if not token:
            return None
        with self._lock:
            self._drain()
            self._sweep_locked()
            ticket = self._tickets.get(token)
            if ticket is None:
                return None
            if self._expired_locked(ticket):
                del self._tickets[token]
                return None
            ticket.last_seen = self._clock()
            return self._status_locked(ticket)

    def waiting(self) -> int:
        """How many tokens are still in line."""
        with self._lock:
            self._drain()
            return self._issued - self._admitted

    def stats(self) -> dict:
        with self._lock:
            self._drain()
            return {
                "admit_rate_per_second": self.rate,
                "waiting": self._issued - self._admitted,
                "issued_total": self._issued,
                "admitted_total": self._admitted,
                "live_tokens": len(self._tickets),
            }
