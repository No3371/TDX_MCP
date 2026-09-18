"""ASGI plumbing: make the caller's IP address available to the tools."""

from __future__ import annotations

from contextvars import ContextVar
from typing import List, Optional

_client_ip: ContextVar[str] = ContextVar("client_ip", default="unknown")


def client_ip() -> str:
    return _client_ip.get()


def _headers(scope) -> List[tuple]:
    return scope.get("headers") or []


def extract_ip(scope, trusted_proxy_hops: int) -> str:
    peer = "unknown"
    client = scope.get("client")
    if client:
        peer = client[0]
    if trusted_proxy_hops <= 0:
        return peer

    forwarded = None
    for key, value in _headers(scope):
        if key.lower() == b"x-forwarded-for":
            forwarded = value.decode("latin-1")
            break
    if not forwarded:
        return peer

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return peer
    # With N trusted proxies in front of us, the caller is the (N+1)-th entry
    # from the right; anything further left is client-supplied and untrusted.
    index = len(chain) - trusted_proxy_hops - 1
    return chain[max(index, 0)]


class ClientIPMiddleware:
    """Pure ASGI middleware, so streaming responses are not buffered."""

    def __init__(self, app, trusted_proxy_hops: int = 1) -> None:
        self.app = app
        self.trusted_proxy_hops = trusted_proxy_hops

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _client_ip.set(extract_ip(scope, self.trusted_proxy_hops))
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip.reset(token)
