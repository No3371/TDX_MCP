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
callers are served straight away. No token is issued, nothing is queued, and
the result carries `"served_directly": true`. Waiting callers veto the bypass:
letting a newcomer past a line it does not have to join would starve the
callers already in it.

**Once traffic passes the threshold**, the queue takes over:

1. A call **without a token**, or with a token the relay will not accept, is a
   request for a token. The relay books the next slot in line and answers with
   a token, the queue position and how long to wait.
2. A call **with a token whose slot has not come up** gets the same answer, with
   the current position. The upstream TDX API is not touched.
3. A call **with a token whose slot has passed** is served.

The queue hands out **10 slots per second** (`RELAY_ADMIT_RATE`), paced exactly
1/10 s apart, with the first `RELAY_ADMIT_BURST` admitted at once after an idle
period.

### The token is the queue

A token is a signed statement of when its holder may be served:

```
t1.<key id>.<admit_at, expires_at, nonce>.<HMAC-SHA256, 16 bytes>
```

Issuing one moves a single number — the tail of the line:

```
admit_at = max(now - (burst - 1)/rate, tail)
tail     = admit_at + 1/rate
```

So the relay keeps **no record of any token**, whatever the length of the queue.
Checking one is a signature check and a comparison against the clock, the
position is `(admit_at - now) x rate`, and tokens keep their place across a
restart or across replicas that share the key. What remains is the tail counter
(one float), the per-IP buckets, and two floats for the load average.

Times are wall clock, because a token has to mean the same thing to a process
that did not issue it. The relay's clock needs NTP.

Every served call hands back a **renewed token** with its expiry pushed out
(`RELAY_TOKEN_TTL`, one hour). A token in use never dies; one that falls out of
use dies on its own. That is how the idle timeout slides without storing
anything.

### Wire format

Served while quiet, with no token in play:

```json
{"status": "ok", "served_directly": true, "date": "2026-09-18", "trains": [ … ]}
```

Queued:

```json
{
  "status": "queued",
  "token": "t1.0.AAABoLjq…",
  "new_token": true,
  "queue_position": 37,
  "estimated_wait_seconds": 3.7,
  "retry_after_seconds": 3.9,
  "admit_rate_per_second": 10.0,
  "hint": "Call the same tool again with this token after the wait. …"
}
```

`retry_after_seconds` is padded past the true wait (`RELAY_RETRY_PAD`), so a
caller that obeys the relay is never charged for arriving early.

Served on a token, which is renewed in the answer:

```json
{"status": "ok", "token": "t1.0.AAABoLjr…", "date": "2026-09-18", "trains": [ … ]}
```

Other statuses: `rate_limited` (see below), `busy` (the wait would be longer
than `RELAY_MAX_WAIT`), `upstream_error` (TDX refused or timed out).

## Rate limiting

Two token buckets per IP:

| Bucket | Charged for | Default |
| ------ | ----------- | ------- |
| **faults** | Anything the caller brought on itself: asking for a token, polling before its slot, presenting a token the relay will not accept | 20 back to back, then 1 per second |
| **throughput** | Every request, valid token or not | 50 back to back, then 5 per second |

A caller that waits as it was told pays **one** credit for its token and nothing
after that — served calls and bypassed calls are free. A caller that polls its
queued token in a tight loop pays for every poll and is shut out in about
twenty of them. A rate-limited answer still carries the caller's token and its
position, so being refused does not cost it its place in line.

**A failure of the upstream API is never charged.** An outage at TDX makes every
response a non-success; charging those would lock callers out of the relay on
top of an outage it did not cause.

The second bucket exists because a token is a bearer capability: its holder can
replay it, and nothing stateless can prevent that. The throughput bucket is what
bounds a caller hammering the relay with a perfectly valid token. Set it from
the TDX quota the relay runs on, not from what feels polite.

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
| `RELAY_TOKEN_SECRET` | random per process | HMAC key for tokens. **Set it**: unset means every token dies on restart and a second replica rejects them all |
| `RELAY_TOKEN_KEY_ID` | `0` | Key id carried in the token, for rotation |
| `RELAY_TOKEN_TTL` | `3600` | Life of a token, pushed out on every served call |
| `RELAY_BYPASS_THRESHOLD` | `5` | Requests per second below which the queue is skipped; `0` disables the bypass |
| `RELAY_LOAD_WINDOW` | `10` | Time constant of the moving average, in seconds |
| `RELAY_ADMIT_RATE` | `10` | Slots handed out per second |
| `RELAY_ADMIT_BURST` | = admit rate | Immediate admissions after an idle period |
| `RELAY_MAX_WAIT` | `300` | Longest wait the relay will promise before answering `busy` |
| `RELAY_RETRY_PAD` | `0.2` | Padding added to every advertised wait |
| `RELAY_IP_FAULT_BURST`, `RELAY_IP_FAULT_RATE` | `20`, `1` | Per-IP bucket for caller faults |
| `RELAY_IP_REQUEST_BURST`, `RELAY_IP_REQUEST_RATE` | `50`, `5` | Per-IP bucket for all requests |
| `RELAY_TRUSTED_PROXY_HOPS` | `1` | Proxies in front of the relay |
| `RELAY_ALLOWED_HOSTS`, `RELAY_ALLOWED_ORIGINS` | unset | Comma separated; unset turns DNS rebinding protection off |
| `RELAY_HOST`, `RELAY_PORT` | `0.0.0.0`, `8080` | Listen address |
| `RELAY_PATH_*` | see `paths.py` | Override any TDX API path |

### More than one replica

Give every replica the same `RELAY_TOKEN_SECRET` and they all honour each
other's tokens. The tail counter is still per process, so either give each of
k replicas `RELAY_ADMIT_RATE = N/k` and let them run independently, or move the
tail behind one shared counter. Independent tails are fine: the total rate is
right, only the ordering between replicas is approximate.

### Rotating the key

Signing with a new key id invalidates every token signed with the old one,
which is the only way to revoke tokens in bulk. To rotate without dumping
everyone back in line, keep the old secret loadable for `RELAY_TOKEN_TTL` after
the change — `TokenSigner` takes a `retired` mapping of key id to secret for
exactly that.

## Limits to know before deploying

- **A token cannot be revoked one at a time.** It is a bearer capability, valid
  until it expires, and there is no record of it to delete. The only revocation
  is key rotation, which voids every outstanding token at once. If you need to
  cut off one abuser, the lever is the IP buckets, not the token.
- **A token can be shared.** Nothing binds it to a caller. Binding it to an IP
  was considered and rejected: NAT and mobile hand-offs break it, and it
  protects little. The throughput bucket is the bound that matters.
- **Wall clock matters.** `admit_at` is an absolute time, so a relay with a
  wrong clock admits everyone early or nobody at all, and replicas that disagree
  on the time disagree on the queue.
- **The per-IP buckets are the only thing that still grows with callers.** Rate
  limiting needs memory by definition. The buckets are swept, but if the shape
  bothers you, swap the dict for fixed-size sharded counters.
- **Abandoned tokens still hold their slot.** Someone who asks for a token and
  walks away leaves a gap in the schedule. This is inherent to booking slots
  ahead, and matches "N admissions per second" literally.
- **The TDX paths in `paths.py` are not verified against a live TDX account**
  in this repository. Check them against the TDX swagger, and override with
  `RELAY_PATH_*` where they have moved.
- **Every call is billed to the operator's TDX quota.** The admission rate, the
  throughput bucket and the response cache are what keep that quota in bounds.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

`tests/test_tokens.py`, `tests/test_admission.py`, `tests/test_ratelimit.py`,
`tests/test_meter.py` and `tests/test_gate.py` drive the signer, the queue, the
buckets, the moving average and the charging policy with a fake clock —
including forged and tampered tokens, and a token honoured by a second relay
process that never issued it. `tests/test_server_e2e.py` runs the real server under
uvicorn and talks to it with a real MCP client over streamable HTTP, with the
TDX call stubbed.
