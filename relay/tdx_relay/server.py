"""Open TDX MCP relay.

Anyone may connect.  The relay holds the TDX credential, so access is paced by
an admission queue instead of by per-caller API keys: a call without a valid
token gets a token minted and queued, and the queue admits N tokens per second.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import paths, projection
from .admission import AdmissionQueue
from .config import Settings
from .gate import Gate
from .http import ClientIPMiddleware, client_ip
from .meter import RateMeter
from .ratelimit import RateLimiter
from .tdx import TDXClient, UpstreamError

TAIPEI = ZoneInfo("Asia/Taipei")

def _today() -> str:
    return datetime.now(TAIPEI).strftime("%Y-%m-%d")


def build_server(settings: Optional[Settings] = None) -> MCPServer:
    settings = settings or Settings.from_env()
    queue = AdmissionQueue(
        rate=settings.admit_rate,
        burst=settings.admit_burst,
        admitted_ttl=settings.admitted_ttl,
        queued_ttl=settings.queued_ttl,
        queue_limit=settings.queue_limit,
    )
    limiter = RateLimiter(burst=settings.ip_issue_burst, rate=settings.ip_issue_rate)
    meter = RateMeter(window=settings.load_window)
    gate = Gate(queue, limiter, meter, bypass_threshold=settings.bypass_threshold)
    tdx = TDXClient(settings)

    mcp = MCPServer(
        name="tdx-relay",
        instructions=(
            "Open relay for TDX transport data. While the relay is quiet, call "
            "the tools directly: no token is needed. When it is busy a call "
            "without a token comes back with status 'queued', a token and a "
            "wait in seconds; wait that long, then call again with the same "
            "token and reuse it for every later call."
        ),
    )

    async def serve(token: Optional[str], fetch) -> Dict[str, Any]:
        decision = gate.admit(token, client_ip())
        if not decision.admitted:
            return decision.payload
        try:
            data = await fetch()
        except UpstreamError as exc:
            return {"status": "upstream_error", "token": decision.token, "reason": str(exc)}
        result = {"status": "ok", **data}
        if decision.bypassed:
            result["served_directly"] = True
        else:
            result["token"] = decision.token
        if decision.new_token:
            result["new_token"] = True
            result["hint"] = "Reuse this token on every later call to stay out of the queue."
        return result

    # -- queue ----------------------------------------------------------
    @mcp.tool()
    async def queue_status(token: Optional[str] = None) -> Dict[str, Any]:
        """Check whether the relay needs a token right now, and get one if so.

        Args:
            token: Queue token, if an earlier call returned one. Omit it otherwise. Omit it unless the relay asked for one.
        """
        decision = gate.admit(token, client_ip())
        if not decision.admitted:
            return decision.payload
        if decision.bypassed:
            return {
                "status": "open",
                "message": "The relay is quiet, so no token is needed. Call the tools directly.",
                "requests_per_second": round(gate.load(), 2),
                "bypass_below_requests_per_second": gate.bypass_threshold,
                **queue.stats(),
            }
        return {
            "status": "admitted",
            "token": decision.token,
            "new_token": decision.new_token,
            "requests_per_second": round(gate.load(), 2),
            **queue.stats(),
        }

    # -- TRA ------------------------------------------------------------
    @mcp.tool()
    async def find_tra_station(keyword: str, token: Optional[str] = None) -> Dict[str, Any]:
        """Find Taiwan Railway (TRA) station IDs by name, e.g. '板橋'.

        Args:
            keyword: Part of the station name in Chinese.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(paths.path("tra_station"))
            return {"stations": projection.stations(payload, "Stations", keyword)}

        return await serve(token, fetch)

    @mcp.tool()
    async def search_tra_trains(
        origin: str,
        destination: str,
        date: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List TRA trains between two stations. Returns at most 3 trains.

        Args:
            origin: Origin station ID, from find_tra_station.
            destination: Destination station ID, from find_tra_station.
            date: Travel date as YYYY-MM-DD. Defaults to today in Taipei.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(
                paths.path(
                    "tra_od_timetable",
                    origin=origin,
                    destination=destination,
                    date=date or _today(),
                )
            )
            return {"date": date or _today(), "trains": projection.tra_trains(payload)}

        return await serve(token, fetch)

    @mcp.tool()
    async def get_tra_fare(
        origin: str, destination: str, token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get TRA ticket prices between two stations.

        Args:
            origin: Origin station ID, from find_tra_station.
            destination: Destination station ID, from find_tra_station.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(
                paths.path("tra_od_fare", origin=origin, destination=destination)
            )
            return {"fares": projection.fares(payload, "ODFares")}

        return await serve(token, fetch)

    # -- THSR -----------------------------------------------------------
    @mcp.tool()
    async def find_thsr_station(keyword: str, token: Optional[str] = None) -> Dict[str, Any]:
        """Find High Speed Rail (THSR) station IDs by name, e.g. '左營'.

        Args:
            keyword: Part of the station name in Chinese.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(paths.path("thsr_station"))
            return {"stations": projection.stations(payload, "Stations", keyword)}

        return await serve(token, fetch)

    @mcp.tool()
    async def search_thsr_trains(
        origin: str,
        destination: str,
        date: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List THSR trains between two stations. Returns at most 3 trains.

        Args:
            origin: Origin station ID, from find_thsr_station.
            destination: Destination station ID, from find_thsr_station.
            date: Travel date as YYYY-MM-DD. Defaults to today in Taipei.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(
                paths.path(
                    "thsr_od_timetable",
                    origin=origin,
                    destination=destination,
                    date=date or _today(),
                )
            )
            return {"date": date or _today(), "trains": projection.thsr_trains(payload)}

        return await serve(token, fetch)

    @mcp.tool()
    async def get_thsr_fare(
        origin: str, destination: str, token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get THSR ticket prices between two stations.

        Args:
            origin: Origin station ID, from find_thsr_station.
            destination: Destination station ID, from find_thsr_station.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(
                paths.path("thsr_od_fare", origin=origin, destination=destination)
            )
            return {"fares": projection.fares(payload, "ODFares")}

        return await serve(token, fetch)

    # -- road events ----------------------------------------------------
    @mcp.tool()
    async def get_city_road_events(city: str, token: Optional[str] = None) -> Dict[str, Any]:
        """Road events on city roads. Returns at most 5 events.

        Args:
            city: City in TDX spelling, e.g. 'Taipei', 'Taoyuan', 'Kaohsiung'.
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(paths.path("event_city", city=city))
            return {"city": city, "events": projection.events(payload)}

        return await serve(token, fetch)

    @mcp.tool()
    async def get_provincial_highway_events(token: Optional[str] = None) -> Dict[str, Any]:
        """Road events on provincial highways. Returns at most 5 events.

        Args:
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(paths.path("event_provincial"))
            return {"events": projection.events(payload)}

        return await serve(token, fetch)

    @mcp.tool()
    async def get_freeway_events(token: Optional[str] = None) -> Dict[str, Any]:
        """Road events on freeways (國道). Returns at most 5 events.

        Args:
            token: Queue token, if an earlier call returned one. Omit it otherwise.
        """

        async def fetch():
            payload = await tdx.get(paths.path("event_freeway"))
            return {"events": projection.events(payload)}

        return await serve(token, fetch)

    mcp._relay = {
        "queue": queue,
        "limiter": limiter,
        "meter": meter,
        "gate": gate,
        "tdx": tdx,
        "settings": settings,
    }
    return mcp


def build_app(settings: Optional[Settings] = None):
    """Build the ASGI app: MCP over streamable HTTP at /mcp."""
    settings = settings or Settings.from_env()
    mcp = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # pragma: no cover - trivial
        from starlette.responses import JSONResponse

        return JSONResponse(
            {
                "ok": True,
                "requests_per_second": round(mcp._relay["meter"].value(), 2),
                "bypass_below_requests_per_second": mcp._relay["gate"].bypass_threshold,
                **mcp._relay["queue"].stats(),
            }
        )

    app = mcp.streamable_http_app(
        stateless_http=True,
        transport_security=transport_security(settings),
        host=settings.host,
    )
    # Outermost middleware, so the caller IP is set before anything else runs.
    app.add_middleware(ClientIPMiddleware, trusted_proxy_hops=settings.trusted_proxy_hops)
    app.state.relay = mcp._relay
    return app


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """A public relay is reached under whatever host name it is deployed at.

    Set RELAY_ALLOWED_HOSTS / RELAY_ALLOWED_ORIGINS to keep DNS rebinding
    protection on; leave them unset to accept any Host header.
    """
    import os

    hosts = [h for h in os.environ.get("RELAY_ALLOWED_HOSTS", "").split(",") if h]
    origins = [o for o in os.environ.get("RELAY_ALLOWED_ORIGINS", "").split(",") if o]
    if not hosts and not origins:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)
