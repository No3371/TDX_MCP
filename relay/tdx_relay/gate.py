"""The admission gate every relay tool call goes through.

Two per-IP buckets guard it:

* **faults** — charged for anything the caller brought on itself: asking for a
  token, polling before its admission time, presenting a bad token.  A caller
  that waits as it was told pays once for its token and nothing after that.
  A failure of the upstream API is never charged: an outage at TDX must not
  lock callers out of the relay as well.
* **throughput** — charged for every request, with a much larger allowance.
  A token is a bearer capability, so nothing stateless can stop its holder
  replaying it; this bucket is what bounds a caller that hammers the relay
  with a perfectly valid token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .admission import Admitter, QueueFull, State
from .meter import RateMeter
from .ratelimit import RateLimiter

_WAIT_HINT = (
    "Call the same tool again with this token after the wait. Keep the token: "
    "it holds your place in line. Calling back early costs you against this "
    "IP address's limit."
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
        admitter: Admitter,
        fault_limiter: RateLimiter,
        throughput_limiter: Optional[RateLimiter] = None,
        meter: Optional[RateMeter] = None,
        bypass_threshold: float = 0.0,
        retry_pad: float = 0.2,
    ) -> None:
        self.admitter = admitter
        self.faults = fault_limiter
        self.throughput = throughput_limiter
        self.meter = meter or RateMeter()
        # Below this many requests per second the queue is out of the way
        # entirely.  0 keeps every caller on the token path.
        self.bypass_threshold = bypass_threshold
        # Advertised waits are padded, so a caller that obeys the relay is not
        # charged a fault for arriving a few milliseconds early.
        self.retry_pad = retry_pad

    def admit(self, token: Optional[str], ip: str) -> Decision:
        """Decide whether this caller may be served now."""
        load = self.meter.record()

        if self.throughput is not None:
            allowed, retry_after = self.throughput.take(ip)
            if not allowed:
                return self._refused(
                    "too many requests from this IP address", retry_after, token
                )

        status = self.admitter.check(token)

        if status is not None and status.state is State.ADMITTED:
            claims = self.admitter.claims(token)
            # Hand back a token with its expiry pushed out, so a token in use
            # never dies and one that falls out of use does.
            return Decision(True, self.admitter.renew(claims) if claims else token)

        if self._quiet(load):
            # A token buys a place in a line that nobody is standing in.
            return Decision(True, None, False, True)

        # Everything from here is a caller fault: an early poll, or a token the
        # relay will not accept and has to replace.
        allowed, retry_after = self.faults.take(ip)
        if not allowed:
            return self._refused(
                "too many early or tokenless calls from this IP address", retry_after, token
            )

        if status is not None:
            return Decision(False, status.token, False, False, self._queued(status))

        try:
            status = self.admitter.issue()
        except QueueFull:
            return Decision(
                False,
                None,
                False,
                False,
                {
                    "status": "busy",
                    "reason": f"the wait is longer than {self.admitter.max_wait:.0f} seconds",
                    "retry_after_seconds": round(self.admitter.max_wait, 1),
                },
            )

        if status.state is State.ADMITTED:
            return Decision(True, status.token, True)
        return Decision(False, status.token, True, False, self._queued(status))

    # -- payloads ---------------------------------------------------------
    def _queued(self, status) -> Dict[str, Any]:
        return {
            "status": "queued",
            "token": status.token,
            "new_token": status.new_token,
            "queue_position": status.position,
            "estimated_wait_seconds": round(status.eta_seconds, 1),
            "retry_after_seconds": round(status.eta_seconds + self.retry_pad, 1),
            "admit_rate_per_second": self.admitter.rate,
            "hint": _WAIT_HINT,
        }

    def _refused(self, reason: str, retry_after: float, token: Optional[str]) -> Decision:
        payload: Dict[str, Any] = {
            "status": "rate_limited",
            "reason": reason,
            "retry_after_seconds": round(retry_after, 1),
            "hint": "Wait for the time given, and keep any token you already hold.",
        }
        # A rate limited caller keeps its place in line, so give the token back
        # rather than making it ask for another one later.
        status = self.admitter.check(token)
        if status is not None:
            payload["token"] = status.token
            payload["queue_position"] = status.position
            payload["retry_after_seconds"] = round(
                max(retry_after, status.eta_seconds + self.retry_pad), 1
            )
        return Decision(False, status.token if status else None, False, False, payload)

    # -- load -------------------------------------------------------------
    def _quiet(self, load: float) -> bool:
        """Is the relay quiet enough to skip the queue?

        Waiting callers veto the bypass: letting a newcomer past a line it
        does not have to join would starve the callers already in it.
        """
        if self.bypass_threshold <= 0:
            return False
        return load < self.bypass_threshold and self.admitter.waiting() == 0

    def load(self) -> float:
        return self.meter.value()
