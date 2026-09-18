"""Upstream TDX client.

The relay holds one operator credential and calls TDX on behalf of everyone,
so this client protects the operator's quota: one shared access token with
refresh, a concurrency cap, and a short response cache.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from .config import Settings


class UpstreamError(Exception):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class TDXClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(timeout=settings.upstream_timeout)
        self._sem = asyncio.Semaphore(settings.upstream_concurrency)
        self._auth_lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth -----------------------------------------------------------
    async def _token(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._auth_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._token_expires_at:
                return self._access_token
            if not self._s.client_id or not self._s.client_secret:
                raise UpstreamError("relay is not configured with TDX credentials")
            resp = await self._client.post(
                self._s.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._s.client_id,
                    "client_secret": self._s.client_secret,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                raise UpstreamError("TDX authentication failed", resp.status_code)
            payload = resp.json()
            self._access_token = payload["access_token"]
            # Refresh a minute early so in-flight calls never race the expiry.
            self._token_expires_at = time.monotonic() + max(60.0, float(payload.get("expires_in", 86400)) - 60.0)
            return self._access_token

    # -- requests -------------------------------------------------------
    def _cache_key(self, path: str, params: Mapping[str, Any]) -> Tuple[str, str]:
        return path, "&".join(f"{k}={params[k]}" for k in sorted(params))

    async def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        params = dict(params or {})
        params.setdefault("$format", "JSON")
        key = self._cache_key(path, params)
        now = time.monotonic()

        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

        url = f"{self._s.api_base}{path}"
        async with self._sem:
            data = await self._get_once(url, params)

        if self._s.cache_ttl > 0:
            if len(self._cache) > 5000:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self._s.cache_ttl, data)
        return data

    async def _get_once(self, url: str, params: Mapping[str, Any]) -> Any:
        token = await self._token()
        resp = await self._request(url, params, token)
        if resp.status_code == 401:
            token = await self._token(force=True)
            resp = await self._request(url, params, token)
        if resp.status_code == 429:
            raise UpstreamError("TDX rate limit reached, try again shortly", 429)
        if resp.status_code >= 400:
            raise UpstreamError(f"TDX returned HTTP {resp.status_code}", resp.status_code)
        return resp.json()

    async def _request(self, url: str, params: Mapping[str, Any], token: str) -> httpx.Response:
        try:
            return await self._client.get(
                url,
                params=params,
                headers={
                    "authorization": f"Bearer {token}",
                    "accept-encoding": "gzip",
                },
            )
        except httpx.HTTPError as exc:  # network level
            raise UpstreamError(f"cannot reach TDX: {exc}") from exc
