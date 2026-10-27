# Kowan architecture


A Docker management platform written in **Python** (asyncio): a control plane
that serves a REST API and a React SPA, an agent per host, and a CLI.
Frameworks: **aiohttp** (REST, WebSocket, the static SPA), **pydantic v2**
(models), stdlib **sqlite3** (store), **cryptography** (secret encryption and
the agent's self-signed TLS), **typer** (CLI). Outbound HTTP from the server
and the agent uses aiohttp's client through `kowan/httpcap.py` (every response
body read up to a cap); `kowanctl` uses the stdlib (`kowan/cli/httpclient.py`).
Passwords
use **scrypt**, JWTs are **HS256** written by hand, and Docker is reached
through the **Engine API** on its unix socket: no dependencies beyond
`requirements.txt` (no fastapi, docker-py, bcrypt or pyjwt).

The transport is built on an **in-house stream multiplexer** and a
**bidirectional server↔agent tunnel** with token authentication, because Python
has no light, maintained equivalent of `hashicorp/yamux`.

```
kowan/
  mux/         # symmetric multiplexer (stdlib asyncio only): frame/stream/session
  transport/   # the message pipe: memory (tests), websocket, aiohttp_ws
  tunnel/      # token-based host tunnel: handshake/endpoint/hub (stoa + atos)
  config.py errors.py events.py backup.py
  models/      # pydantic models (camelCase aliases), _serde.drop_empty for omitempty
  store/       # sqlite3 Store, full schema + CRUD (mixins)
  auth/        # scrypt + JWT HS256 + API keys + middleware/require_role
  web/         # HTTP helpers + Services (DI) + create_app + middleware + SPA
  docker/      # Ops + LocalClient (Engine API) + RemoteClient (mux) + Provider
  agentproto.py agent_ops_server.py   # docker-over-mux RPC + the agent's dispatcher
  api/         # REST modules (register(app, svc)) mounted under /api/v1
  deployment/ authn/ secrets/ registry/ notify/ workflow/ triggers/ monitor/
  server.py    # control plane (python -m kowan.server)
  agent.py     # edge/direct agent (python -m kowan.agent)
  cli/         # kowanctl (python -m kowan.cli)
tests/         # stdlib tests (mux/tunnel + REST end to end + docker-over-tunnel)
examples/      # ws_demo.py (the mux over a real WebSocket)
web/           # frontend/ (React sources) + dist/ (the built SPA the server serves)
```

## Running it

```bash
make deps          # pip install -r requirements.txt
make frontend      # build the SPA into web/dist
make run           # http://localhost:5081; the first run asks for the setup token in the log
```

The server reads `web/dist` from disk at runtime, so rebuilding the frontend is
enough to update the UI. Real Docker operations need a daemon
(`/var/run/docker.sock`); on macOS with OrbStack or Docker Desktop the socket
points at the local daemon.

## The two key ideas

**1. A mux is a two-way pipe.** Once the byte stream is up, *both* sides can
open streams to the other. The only thing to avoid is `stream_id` collisions:
the "client" side uses **odd** ids, the "server" side **even** ids.

**2. Who dials the socket is not who opens the streams.** The **control plane
is always the client mux** (it opens streams for operations on the host) and the
**agent is always the server mux** (it opens streams to push events and
metrics), *whoever* opened the connection. That is what gives one code path for
both setups.

### The two (independent) axes

| Axis | Values | Decided by |
|------|--------|-----------|
| Who dials | sends vs checks the **token** | the connection mode |
| Who is the control plane | client mux (odd ids) vs server mux (even ids) | the role, not who dials |

### The two connection modes

| Mode | Who dials | Token | Needs an address? | Who reconnects |
|------|-----------|-------|-------------|----------------|
| **atos** (edge / behind NAT) | the **agent** → server | the agent **sends** it, the server **checks** it | no | the agent (with backoff) |
| **stoa** (direct) | the **server** → agent | the server **sends** it, the agent **checks** it | **yes** (the agent's address) | the server |

> **There is one token rule: whoever dialed the socket sends the token,
> whoever accepted it checks it.** So the agent *always* has the token
> configured (it sends it in atos, checks it in stoa), and one environment
> variable switches the mode. The agent-side check in stoa is a second line of
> defence: a compromised server cannot drive the agent without the right token.

In direct mode the agent serves **wss://** with an ephemeral self-signed
certificate (P-256) generated at startup, unless you provide one with
`KOWAN_AGENT_TLS_CERT` / `KOWAN_AGENT_TLS_KEY`. The control plane connects
without verifying the certificate; the token handshake is bound to its
fingerprint instead.

## The frame format

One frame per transport message (one WebSocket binary message is one frame, so
no extra framing).

```
0        1        2                   6                   10
+--------+--------+-------------------+-------------------+
|  type  | flags  |     stream_id     |      length       |  payload...
+--------+--------+-------------------+-------------------+
```

- `type`: `DATA` | `WINDOW_UPDATE` | `PING` | `GO_AWAY`
- `flags`: `SYN` (open a stream) · `FIN` (half-close) · `RST` (abort)
- **Credit-based flow control**: every stream starts with a fixed window; a
  writer cannot exceed its `send_window` and blocks (backpressure) until the
  reader, *consuming* the bytes, gives credit back with a `WINDOW_UPDATE`. So a
  slow stream (say, following logs) does not starve the others and memory stays
  bounded.

## Trying it

```bash
# tests (stdlib only, nothing to install)
make test
# or: python3 -m unittest discover -s tests -t . -v

# end-to-end demo over a real WebSocket (aiohttp)
PYTHONPATH=. python3 examples/ws_demo.py
```

On a single WebSocket dialed by the agent, the demo shows
`control plane → agent` (a host operation) **and** `agent → control plane` (an
event push), both multiplexed.

## Status

The control plane boots and serves the whole REST surface on a real SQLite
database; the agent and the CLI start; the docker-over-tunnel path is verified
end to end against real Docker (a host and a VM). The project was ported one to
one from a Go version (since removed), and a parity audit before the removal
closed 10 differences.

**Still to harden**, mostly against real services: WebSocket streaming (logs,
stats, exec) is implemented but has little end-to-end coverage; some external
providers (live LDAP, cloud secret managers) are implemented but not tested
against the real services.

## Environment variables: retention and performance

| Variable | Default | Effect |
|---|---|---|
| `KOWAN_AUDIT_RETENTION` | `90d` | Delete `audit_log` entries older than this. |
| `KOWAN_ALERT_RETENTION` | `30d` | Delete **resolved** alerts older than this (active ones stay). |
| `KOWAN_WORKFLOW_RUNS_KEEP` | `100` | Runs kept per workflow (the most recent; a run still `running` is never deleted). |
| `KOWAN_UVLOOP` | (on) | `0` turns uvloop off and uses the standard asyncio loop. |

Durations accept Go's format (`720h`, `1h30m`), days (`90d`) or whole seconds;
`0` turns that cleanup off. Pruning runs about once an hour in the monitor's
loop. `orjson` and `uvloop` are optional: without them the stdlib `json` and
`asyncio` are used.

## License

MIT, see [LICENSE](../LICENSE). Kowan is free software: no private dependencies, no account required.
