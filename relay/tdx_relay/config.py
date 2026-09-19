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
    admit_burst: float = 10.0         # immediate admissions an idle relay hands out
    max_wait: float = 300.0           # longest wait the relay will promise
    token_ttl: float = 3600.0         # life of a token, pushed out on every served call
    retry_pad: float = 0.2            # padding added to every advertised wait
    token_secret: str = ""            # HMAC key; random per process when unset
    token_key_id: str = "0"

    # --- Per-IP limits -------------------------------------------------
    # Caller faults: asking for a token, polling early, presenting a bad one.
    ip_fault_burst: float = 20.0
    ip_fault_rate: float = 1.0
    # Every request, valid token or not.  Bounds a caller replaying a token.
    ip_request_burst: float = 50.0
    ip_request_rate: float = 5.0
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
            max_wait=_env_float("RELAY_MAX_WAIT", cls.max_wait),
            token_ttl=_env_float("RELAY_TOKEN_TTL", cls.token_ttl),
            retry_pad=_env_float("RELAY_RETRY_PAD", cls.retry_pad),
            token_secret=os.environ.get("RELAY_TOKEN_SECRET", ""),
            token_key_id=os.environ.get("RELAY_TOKEN_KEY_ID", cls.token_key_id),
            ip_fault_burst=_env_float("RELAY_IP_FAULT_BURST", cls.ip_fault_burst),
            ip_fault_rate=_env_float("RELAY_IP_FAULT_RATE", cls.ip_fault_rate),
            ip_request_burst=_env_float("RELAY_IP_REQUEST_BURST", cls.ip_request_burst),
            ip_request_rate=_env_float("RELAY_IP_REQUEST_RATE", cls.ip_request_rate),
            trusted_proxy_hops=_env_int("RELAY_TRUSTED_PROXY_HOPS", cls.trusted_proxy_hops),
            host=os.environ.get("RELAY_HOST", cls.host),
            port=_env_int("RELAY_PORT", cls.port),
        )
