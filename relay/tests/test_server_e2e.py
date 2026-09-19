"""End to end: a real MCP client over HTTP against the relay, TDX stubbed out."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from contextlib import contextmanager

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from tdx_relay.config import Settings
from tdx_relay.http import ClientIPMiddleware
from tdx_relay.server import build_server

FAKE_TIMETABLE = {
    "TrainTimetables": [
        {
            "TrainInfo": {"TrainNo": "149", "TrainTypeName": {"Zh_tw": "自強"}},
            "StopTimes": [
                {"StationName": {"Zh_tw": "板橋"}, "DepartureTime": "19:05"},
                {"StationName": {"Zh_tw": "桃園"}, "ArrivalTime": "19:32"},
            ],
        }
    ]
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running(**overrides):
    """Run the real relay under uvicorn, with the TDX call stubbed out."""
    defaults = dict(
        client_id="x",
        client_secret="y",
        admit_rate=1.0,
        admit_burst=1.0,
        ip_fault_burst=5.0,
        ip_fault_rate=0.0001,
        ip_request_burst=1000.0,
        ip_request_rate=1000.0,
        token_secret="e2e-test-key",
        trusted_proxy_hops=1,
        bypass_threshold=0.0,   # off by default here, so the queue is exercised
    )
    settings = Settings(**{**defaults, **overrides})
    mcp = build_server(settings)

    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return FAKE_TIMETABLE

    mcp._relay["tdx"].get = fake_get

    app = mcp.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.add_middleware(ClientIPMiddleware, trusted_proxy_hops=1)

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp", mcp, calls
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def relay():
    with running() as ctx:
        yield ctx


@pytest.fixture
def quiet_relay():
    with running(bypass_threshold=50.0) as ctx:
        yield ctx


async def call(url, name, args, ip="203.0.113.7"):
    http = create_mcp_http_client(headers={"x-forwarded-for": ip})
    async with streamable_http_client(url, http_client=http) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_queue_then_serve(relay):
    url, mcp, calls = relay

    first = await call(url, "search_tra_trains", {"origin": "1020", "destination": "1040"})
    assert first["status"] == "ok"          # relay is idle, so the first call is served
    assert first["new_token"] is True
    assert first["trains"][0]["train_no"] == "149"

    second = await call(url, "search_tra_trains", {"origin": "1020", "destination": "1040"})
    assert second["status"] == "queued"
    assert second["queue_position"] == 1
    token = second["token"]
    assert len(calls) == 1                  # a queued caller never reaches TDX

    await asyncio.sleep(1.1)
    third = await call(url, "search_tra_trains", {"origin": "1020", "destination": "1040", "token": token})
    assert third["status"] == "ok"
    assert third["token"] != token          # every served call renews the token
    assert len(calls) == 2

    fourth = await call(url, "search_tra_trains", {"origin": "1020", "destination": "1040", "token": third["token"]})
    assert fourth["status"] == "ok"


@pytest.mark.anyio
async def test_caller_faults_are_charged_per_ip(relay):
    url, mcp, _ = relay
    for _ in range(5):
        await call(url, "queue_status", {}, ip="198.51.100.1")
    limited = await call(url, "queue_status", {}, ip="198.51.100.1")
    assert limited["status"] == "rate_limited"

    other = await call(url, "queue_status", {}, ip="198.51.100.2")
    assert other["status"] in {"queued", "admitted"}


@pytest.mark.anyio
async def test_a_token_survives_a_restart_of_the_relay(relay):
    url, _, _ = relay
    await call(url, "queue_status", {})                    # takes the first slot
    queued = await call(url, "queue_status", {})
    assert queued["status"] == "queued"
    token = queued["token"]

    # A second relay process, same signing key, no shared memory.
    with running(admit_rate=1.0, token_secret="e2e-test-key") as (other_url, _, _):
        await asyncio.sleep(1.2)
        served = await call(other_url, "search_tra_trains", {"origin": "1", "destination": "2", "token": token})
    assert served["status"] == "ok"


@pytest.mark.anyio
async def test_tools_are_listed_with_a_token_argument(relay):
    url, _, _ = relay
    async with streamable_http_client(url) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    assert {"queue_status", "search_tra_trains", "get_freeway_events"} <= names
    for tool in tools.tools:
        assert "token" in tool.input_schema["properties"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_a_quiet_relay_serves_without_any_token(quiet_relay):
    url, _, calls = quiet_relay

    for _ in range(3):
        result = await call(url, "search_tra_trains", {"origin": "1020", "destination": "1040"})
        assert result["status"] == "ok"
        assert result["served_directly"] is True
        assert "token" not in result
    assert len(calls) == 3


@pytest.mark.anyio
async def test_queue_status_says_no_token_is_needed_while_quiet(quiet_relay):
    url, _, _ = quiet_relay
    result = await call(url, "queue_status", {})
    assert result["status"] == "open"
    assert result["bypass_below_requests_per_second"] == 50.0
    assert "token" not in result
