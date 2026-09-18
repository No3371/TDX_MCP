"""The admission gate every relay tool call goes through."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .admission import AdmissionQueue, QueueFull, State
from .meter import RateMeter
from .ratelimit import RateLimiter

_WAIT_HINT = (
    "Call the same tool again with this token after the wait. "
    "Keep the token: it holds your place in line and stays valid while you use it."
)


@dataclass(frozen=True)
class Decision:
    admitted: bool
    token: Optional[str] = None
    new_token: bool = False
    bypassed: bool = False                     # served without joining the queue
    payload: Optional[Dict[str, Any]] = None   # set when not admitted


class Gate:
    def __init__(
        self,
        queue: AdmissionQueue,
        limiter: RateLimiter,
        meter: Optional[RateMeter] = None,
        bypass_threshold: float = 0.0,
    ) -> None:
        self.queue = queue
        self.limiter = limiter
        self.meter = meter or RateMeter()
        # Below this many requests per second the queue is out of the way
        # entirely.  0 keeps every caller on the token path.
        self.bypass_threshold = bypass_threshold

    def admit(self, token: Optional[str], ip: str) -> Decision:
        """Decide whether this caller may be served now.

        While the moving average of requests stays below ``bypass_threshold``
        and nobody is waiting, callers are served on the spot and no token is
        minted at all.  Once traffic picks up the queue takes over: a caller
        that is not admitted gets back its token, its place in line and when to
        come back, and a caller admitted on a freshly minted token is served
        straight away with the token riding along in the result.
        """
        load = self.meter.record()
        status = self.queue.check(token)

        if status is not None:
            if status.state is State.ADMITTED:
                return Decision(True, status.token)
            return Decision(False, status.token, False, False, self._queued(status, new_token=False))

        if self._quiet(load):
            # A token buys a place in a line that nobody is standing in.
            return Decision(True, None, False, True)

        # No token, or one that is unknown or expired: this is a request for a
        # new token, so it is charged against the caller's IP.
        allowed, retry_after = self.limiter.take(ip)
        if not allowed:
            return Decision(
                False,
                None,
                False,
                False,
                {
                    "status": "rate_limited",
                    "reason": "too many token requests from this IP address",
                    "retry_after_seconds": round(retry_after, 1),
                    "hint": "Reuse the token you were given instead of asking for a new one.",
                },
            )

        try:
            status = self.queue.issue(ip)
        except QueueFull:
            return Decision(
                False,
                None,
                False,
                False,
                {
                    "status": "busy",
                    "reason": "the waiting line is full",
                    "retry_after_seconds": 60,
                },
            )

        if status.state is State.ADMITTED:
            return Decision(True, status.token, True)
        return Decision(False, status.token, True, False, self._queued(status, new_token=True))

    def _quiet(self, load: float) -> bool:
        """Is the relay quiet enough to skip the queue?

        Waiting callers veto the bypass: letting a newcomer past a queue it
        does not have to join would starve the callers already in it.
        """
        if self.bypass_threshold <= 0:
            return False
        return load < self.bypass_threshold and self.queue.waiting() == 0

    def load(self) -> float:
        return self.meter.value()

    def _queued(self, status, new_token: bool) -> Dict[str, Any]:
        return {
            "status": "queued",
            "token": status.token,
            "new_token": new_token,
            "queue_position": status.position,
            "estimated_wait_seconds": round(status.eta_seconds, 1),
            "retry_after_seconds": round(status.eta_seconds, 1),
            "admit_rate_per_second": self.queue.rate,
            "hint": _WAIT_HINT,
        }
