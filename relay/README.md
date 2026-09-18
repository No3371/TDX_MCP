# TDX MCP Relay

An open, remote MCP server in front of the TDX APIs. Anyone may connect: the
relay holds one TDX credential of its own, so callers do not supply `cid`/`cst`
headers. Access is paced by an admission queue instead.

This is a different deployment model from the TDX MCP services described in the
[top-level README](../README.md), where every caller brings a TDX member key and
the calls are billed to that member.

## How the queue works

Every tool takes an optional `token` argument, and the relay only asks for one
when it is busy.

**While the relay is quiet** — the moving average of requests is below
`RELAY_BYPASS_THRESHOLD`, 5 per second by default, and nobody is waiting —
callers are served straight away. No token is minted, nothing is queued, and
the result carries `"served_directly": true`. Waiting callers veto the bypass:
letting a newcomer past a line it does not have to join would starve the
callers already in it.

**Once traffic passes the threshold**, the queue takes over:

1. A call **without a token**, or with a token the relay does not know, is a
   request for a token. The relay mints one, puts it at the back of the line and
   answers with the token, the queue position and how long to wait.
2. A call **with a token that is still waiting** gets the same answer, with the
   current position. The upstream TDX API is not touched.
3. A call **with an admitted token** is served.

The queue admits **10 tokens per second** in issue order (`RELAY_ADMIT_RATE`).
A token already in hand keeps working when traffic dies down again, so a caller
that queued during a rush is never sent back to the end of the line.

The load figure is an exponentially weighted moving average with a 10 second
time constant (`RELAY_LOAD_WINDOW`): old traffic fades instead of falling off a
cliff at a window boundary. It is reported by `queue_status` and `/healthz`.

An admitted token stays valid while it is used, and expires one hour after the
last call (`RELAY_ADMITTED_TTL`). A token that is still waiting after 15 minutes
is forgotten (`RELAY_QUEUED_TTL`).

Admission is computed lazily: each token carries the sequence number it was
issued with, and is admitted once the drain watermark passes it. There is no
background task, position lookup is O(1), and the whole thing is driven by an
injectable clock, which is what the tests use.

### Wire format

Served while quiet, with no token in play:

```json
{"status": "ok", "served_directly": true, "date": "2026-09-18", "trains": [ … ]}
```

Queued:

```json
{
  "status": "queued",
  "token": "kA1s…",
  "new_token": true,
  "queue_position": 37,
  "estimated_wait_seconds": 3.7,
  "retry_after_seconds": 3.7,
  "admit_rate_per_second": 10.0,
  "hint": "Call the same tool again with this token after the wait. …"
}
```

Served on a token:

```json
{"status": "ok", "token": "kA1s…", "date": "2026-09-18", "trains": [ … ]}
```

Other statuses: `rate_limited` (too many token requests from one IP),
`busy` (the waiting line is full), `upstream_error` (TDX refused or timed out).

## Rate limiting

Only **token issuance** is limited per IP, by a token bucket: 3 requests back to
back, then one more every 10 seconds (`RELAY_IP_BURST`, `RELAY_IP_RATE`). A
caller that keeps its token is never charged again, so the limit falls on
callers that throw tokens away and keep asking for new ones.

Behind a proxy, set `RELAY_TRUSTED_PROXY_HOPS` to the number of proxies in
front of the relay. The caller is read as the (hops + 1)-th entry from the right
of `X-Forwarded-For`; anything further left is client-supplied and ignored.
With `0`, the socket peer is used and `X-Forwarded-For` is not read at all.

## Tools

| Tool | What it returns |
| ---- | --------------- |
| `queue_status` | Whether a token is needed at all, and where an existing one stands |
| `find_tra_station` / `find_thsr_station` | Station IDs matching a Chinese name |
| `search_tra_trains` / `search_thsr_trains` | Up to 3 trains for an OD pair and date |
| `get_tra_fare` / `get_thsr_fare` | Ticket prices for an OD pair |
| `get_city_road_events` | Up to 5 events on city roads |
| `get_provincial_highway_events` | Up to 5 events on provincial highways |
| `get_freeway_events` | Up to 5 events on freeways |

Responses are projected down to a few fields per record, as in the upstream TDX
MCP services, to keep the token cost of a result small.

Booking guidance (導訂票) is not relayed. Those TDX services are approved per
member, and an open relay cannot carry one member's approval on behalf of
everyone.

## Running it

```bash
pip install -e .
export TDX_CLIENT_ID=... TDX_CLIENT_SECRET=...
python -m tdx_relay          # MCP at http://0.0.0.0:8080/mcp, health at /healthz
```

Connect to it like any remote MCP server — no headers needed:

```bash
claude mcp add --transport http tdx-relay https://<your-host>/mcp
```

### Settings

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `TDX_CLIENT_ID`, `TDX_CLIENT_SECRET` | — | The relay's own TDX credential |
| `TDX_API_BASE` | `https://tdx.transportdata.tw/api/basic` | Upstream base URL |
| `TDX_AUTH_URL` | TDX Keycloak token endpoint | OAuth2 token endpoint |
| `TDX_CACHE_TTL` | `30` | Seconds an upstream response is reused |
| `TDX_CONCURRENCY` | `8` | Upstream calls in flight |
| `RELAY_BYPASS_THRESHOLD` | `5` | Requests per second below which the queue is skipped; `0` disables the bypass |
| `RELAY_LOAD_WINDOW` | `10` | Time constant of the moving average, in seconds |
| `RELAY_ADMIT_RATE` | `10` | Tokens admitted per second |
| `RELAY_ADMIT_BURST` | = admit rate | Admissions an idle relay may bank |
| `RELAY_ADMITTED_TTL` | `3600` | Idle life of an admitted token |
| `RELAY_QUEUED_TTL` | `900` | Life of a token that never came back |
| `RELAY_QUEUE_LIMIT` | `100000` | Waiting tokens before the relay says `busy` |
| `RELAY_IP_BURST` | `3` | Token requests one IP may make back to back |
| `RELAY_IP_RATE` | `0.1` | Sustained token requests per second per IP |
| `RELAY_TRUSTED_PROXY_HOPS` | `1` | Proxies in front of the relay |
| `RELAY_ALLOWED_HOSTS`, `RELAY_ALLOWED_ORIGINS` | unset | Comma separated; unset turns DNS rebinding protection off |
| `RELAY_HOST`, `RELAY_PORT` | `0.0.0.0`, `8080` | Listen address |
| `RELAY_PATH_*` | see `paths.py` | Override any TDX API path |

## Limits to know before deploying

- **State is per process.** The queue, the tokens and the IP buckets live in
  memory, so two replicas admit 10 per second *each* and do not recognise each
  other's tokens. Run one process, or move `AdmissionQueue` and `RateLimiter`
  behind Redis.
- **The TDX paths in `paths.py` are not verified against a live TDX account**
  in this repository. Check them against the TDX swagger, and override with
  `RELAY_PATH_*` where they have moved.
- **Every call is billed to the operator's TDX quota.** The admission rate and
  the response cache are what keep that quota in bounds; set them to match the
  subscription the relay runs on.
- **A token is a place in line, not an identity.** It is not authentication and
  says nothing about who the caller is.
- **Bypassed calls are not charged to any IP bucket.** The per-IP limit guards
  token issuance, so while the relay is quiet a single IP can use the whole
  bypass allowance. It is bounded by `RELAY_BYPASS_THRESHOLD`: past that the
  queue and the per-IP limit both come back into play.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

`tests/test_admission.py`, `tests/test_ratelimit.py`, `tests/test_meter.py` and
`tests/test_gate.py` drive the queue, the buckets, the moving average and the
bypass with a fake clock. `tests/test_server_e2e.py` runs the real server under
uvicorn and talks to it with a real MCP client over streamable HTTP, with the
TDX call stubbed.
