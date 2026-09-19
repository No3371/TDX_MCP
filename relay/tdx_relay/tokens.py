"""Signed queue tokens.

A token is a signed statement of when its holder may be served:

    t1.<key id>.<payload>.<mac>

The payload packs the admission time, the expiry and a nonce; the MAC is
HMAC-SHA256 over everything before it, truncated to 16 bytes.  Nothing about a
token is stored, so verification is a signature check and two comparisons, and
tokens keep their place in line across a restart.

Times are wall clock (``time.time``), because a token has to mean the same
thing to a process that did not issue it.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
import struct
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Dict, Optional

PREFIX = "t1"
_MAC_BYTES = 16
_PACK = ">QQ4s"   # admit_at ms, expires_at ms, nonce


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Claims:
    admit_at: float      # wall clock second at which the holder may be served
    expires_at: float    # wall clock second after which the token is dead
    nonce: str           # short random id, for logs


class TokenSigner:
    """Signs and verifies tokens, with one live key and any number of old ones.

    Rotate by making the new key current and keeping the previous one in
    ``retired`` until the longest token lifetime has passed.  Dropping a key
    invalidates every token signed with it, which is the only way to revoke
    tokens in bulk.
    """

    def __init__(
        self,
        secret: Optional[bytes] = None,
        key_id: str = "0",
        retired: Optional[Dict[str, bytes]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key_id = key_id
        self._keys: Dict[str, bytes] = dict(retired or {})
        self._keys[key_id] = secret or os.urandom(32)
        self._clock = clock

    def sign(self, admit_at: float, expires_at: float, nonce: Optional[str] = None) -> str:
        nonce_bytes = _unb64(nonce) if nonce else secrets.token_bytes(3)
        payload = struct.pack(
            _PACK, int(admit_at * 1000), int(expires_at * 1000), nonce_bytes.ljust(4, b"\0")
        )
        body = f"{PREFIX}.{self.key_id}.{_b64(payload)}"
        return f"{body}.{_b64(self._mac(self.key_id, body))}"

    def verify(self, token: Optional[str], ignore_expiry: bool = False) -> Optional[Claims]:
        """Return the claims, or None if the token is malformed, forged or expired.

        ``ignore_expiry`` returns the claims of a token that is genuine but out
        of date, which is how the relay tells "your window closed" from
        "this is not one of ours".
        """
        if not token or not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != PREFIX:
            return None
        _, key_id, payload_b64, mac_b64 = parts
        secret = self._keys.get(key_id)
        if secret is None:
            return None
        try:
            payload = _unb64(payload_b64)
            mac = _unb64(mac_b64)
            expected = self._mac(key_id, f"{PREFIX}.{key_id}.{payload_b64}")
        except Exception:
            return None
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            admit_ms, expires_ms, nonce = struct.unpack(_PACK, payload)
        except struct.error:
            return None
        if not ignore_expiry and self._clock() > expires_ms / 1000:
            return None
        return Claims(admit_ms / 1000, expires_ms / 1000, _b64(nonce.rstrip(b"\0")))

    def _mac(self, key_id: str, body: str) -> bytes:
        return hmac.new(self._keys[key_id], body.encode("ascii"), sha256).digest()[:_MAC_BYTES]
