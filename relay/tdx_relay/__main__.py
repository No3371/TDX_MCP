"""Run the relay: python -m tdx_relay"""

from __future__ import annotations

import uvicorn

from .config import Settings
from .server import build_app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        # The relay reads the caller IP itself, with RELAY_TRUSTED_PROXY_HOPS
        # deciding how far down the X-Forwarded-For chain to trust.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
