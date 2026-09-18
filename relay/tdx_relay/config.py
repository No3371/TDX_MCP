"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # --- TDX upstream -------------------------------------------------
    client_id: str = ""
    client_secret: str = ""
    auth_url: str = (
        "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    )
    api_base: str = "https://tdx.transportdata.tw/api/basic"
    upstream_timeout: float = 15.0
    upstream_concurrency: int = 8
    cache_ttl: float = 30.0

    # --- Load bypass ----------------------------------------------------
    bypass_threshold: float = 5.0     # skip the queue below this many requests/s; 0 disables
    load_window: float = 10.0         # time constant of the moving average, in seconds

    # --- Admission queue ----------------------------------------------
    admit_rate: float = 10.0          # tokens admitted per second
    admit_burst: float = 10.0         # unused admission slots that may accumulate
    queue_limit: int = 100_000        # refuse to issue beyond this many waiting tokens
    admitted_ttl: float = 3600.0      # idle admitted token expires after this
    queued_ttl: float = 900.0         # abandoned waiting token is forgotten after this

    # --- Per-IP limit on token issuance -------------------------------
    ip_issue_burst: float = 3.0       # tokens an IP may request back to back
    ip_issue_rate: float = 0.1        # sustained issuance rate per IP (per second)
    trusted_proxy_hops: int = 1       # X-Forwarded-For entries to skip from the right

    # --- HTTP ----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            client_id=os.environ.get("TDX_CLIENT_ID", ""),
            client_secret=os.environ.get("TDX_CLIENT_SECRET", ""),
            auth_url=os.environ.get("TDX_AUTH_URL", cls.auth_url),
            api_base=os.environ.get("TDX_API_BASE", cls.api_base).rstrip("/"),
            upstream_timeout=_env_float("TDX_TIMEOUT", cls.upstream_timeout),
            upstream_concurrency=_env_int("TDX_CONCURRENCY", cls.upstream_concurrency),
            cache_ttl=_env_float("TDX_CACHE_TTL", cls.cache_ttl),
            bypass_threshold=_env_float("RELAY_BYPASS_THRESHOLD", cls.bypass_threshold),
            load_window=_env_float("RELAY_LOAD_WINDOW", cls.load_window),
            admit_rate=_env_float("RELAY_ADMIT_RATE", cls.admit_rate),
            admit_burst=_env_float("RELAY_ADMIT_BURST", _env_float("RELAY_ADMIT_RATE", cls.admit_rate)),
            queue_limit=_env_int("RELAY_QUEUE_LIMIT", cls.queue_limit),
            admitted_ttl=_env_float("RELAY_ADMITTED_TTL", cls.admitted_ttl),
            queued_ttl=_env_float("RELAY_QUEUED_TTL", cls.queued_ttl),
            ip_issue_burst=_env_float("RELAY_IP_BURST", cls.ip_issue_burst),
            ip_issue_rate=_env_float("RELAY_IP_RATE", cls.ip_issue_rate),
            trusted_proxy_hops=_env_int("RELAY_TRUSTED_PROXY_HOPS", cls.trusted_proxy_hops),
            host=os.environ.get("RELAY_HOST", cls.host),
            port=_env_int("RELAY_PORT", cls.port),
        )
