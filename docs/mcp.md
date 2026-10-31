# MCP server

Kowan includes a [Model Context Protocol](https://modelcontextprotocol.io) server, so AI
assistants (Claude Code, Claude Desktop, Cursor and other MCP clients) can inspect and operate
your Docker fleet with the same permissions as a Kowan user or API key.

It is part of the control plane. You don't run or install anything extra, and there are no new
dependencies (the protocol is implemented in `kowan/mcp/`).

- **Endpoint:** `POST <server>/mcp` (Streamable HTTP, MCP revision `2025-06-18`)
- **Auth:** `Authorization: Bearer <API key or login token>`
- **Stdio bridge:** `kowanctl mcp` (for clients that only launch local processes)

## Setup

Create an API key in the console (**API keys** page, `/api-keys`) or with `kowanctl apikey create`.
Its role decides which tools the assistant gets (see [Tools](#tools)). For an assistant
that should only look, a `viewer` key is the safe default.

### Claude Code

```bash
claude mcp add --transport http kowan https://kowan.example.com/mcp \
  --header "Authorization: Bearer pk_..."
```

Then ask, for example: "what's crashing on my servers?" or "show me the last errors of the
api container on host nas".

### Claude Desktop (stdio bridge)

Claude Desktop starts MCP servers as local processes. `kowanctl mcp` is a small bridge: it reads
JSON-RPC from stdin, forwards each message to `/mcp`, and writes the replies to stdout. In
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kowan": {
      "command": "kowanctl",
      "args": ["mcp"],
      "env": {
        "KOWAN_URL": "https://kowan.example.com",
        "KOWAN_API_KEY": "pk_..."
      }
    }
  }
}
```

If `kowanctl` is not on the PATH that Claude Desktop sees, use an absolute path, or
`python -m kowan.cli mcp` with `command` set to the right Python.

The bridge resolves its configuration in this order (first non-empty value wins):

| | |
|---|---|
| server | `--url`, `$KOWAN_URL`, `kowanctl --server`, `$KOWAN_SERVER`, `~/.kowan/config.yaml` |
| credential | `--api-key`, `$KOWAN_API_KEY`, `kowanctl --api-key` / config file, then the `kowanctl login` token |

The bridge keeps the session id, sends the negotiated `MCP-Protocol-Version`, and
re-initializes on its own if the server restarts (404 on the session). On exit it ends the
session. Like the rest of `kowanctl`, it accepts self-signed TLS certificates.

### Cursor and other clients

Any client that supports Streamable HTTP with custom headers works with the URL and header
shown above. For example, Cursor's `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "kowan": {
      "url": "https://kowan.example.com/mcp",
      "headers": { "Authorization": "Bearer pk_..." }
    }
  }
}
```

Clients that only support stdio use the `kowanctl mcp` command shown above.

## Tools

`tools/list` returns only the tools your role may call, and `tools/call` checks the role again.
Every tool that takes `host` accepts a host id or name and defaults to `local`.

| Tool | Role | Annotations | What it does |
|---|---|---|---|
| `fleet_summary` | viewer | read-only | Dashboard view: hosts online, containers running/total, crashed/unhealthy containers with exit codes, open alerts, reclaimable disk |
| `list_hosts` | viewer | read-only | Hosts with live online status |
| `get_host` | viewer | read-only | One host plus Docker engine info |
| `list_containers` | viewer | read-only | Filter by `state`, `name`; `all_hosts=true` for the whole fleet |
| `inspect_container` | viewer | read-only | Summary: image, state, exit code, health, restarts, ports, mounts, networks, env var **names** |
| `container_logs` | viewer | read-only | `tail` (default 200, max 2000), `since`, `grep`, `timestamps` |
| `container_stats` | viewer | read-only | One-shot CPU, memory, network and block I/O |
| `list_images` / `list_volumes` / `list_networks` | viewer | read-only | Paginated lists |
| `recent_events` | viewer | read-only | Docker events (`since` defaults to 1h, max 24h; filter by `type` and `action`) |
| `check_updates` | viewer | read-only, open-world | Image update availability per container (asks the registries) |
| `list_deployments` / `get_deployment` | viewer | read-only | Deployments plus per-target runtime status (env values and webhook secrets hidden) |
| `list_templates` | viewer | read-only | App catalog with its inputs |
| `list_alerts` | viewer | read-only | `status` open (default) / resolved / all |
| `list_workflows` | viewer | read-only | Workflows to use with `run_workflow` |
| `list_workloads` | viewer | read-only | Workloads as the UI groups them: deployments, compose projects/swarm stacks and lone containers, with a state (needs attention first); filter by `host`, `kind`, `state`, `name` |
| `get_workload` | viewer | read-only | One workload by id (`dep~…`, `stack~…`, `ctr~…`) or name: its containers, deployment, log retention and memory trend |
| `workload_resources` | viewer | read-only | CPU and memory history of a workload (`hours` 1–720): summary, up to 96 points, memory trend |
| `memory_growth` | viewer | read-only | Workloads whose memory keeps climbing (likely leaks), steepest first, with hours to the limit |
| `search_logs` | viewer | read-only | Full-text search of the retained log archive (`query`, `workload`, `host`, `since`, `until`); pages back with `before` |
| `list_revisions` / `get_revision` | viewer | read-only | A deployment's revisions (who, when, what changed, which is live) and one revision as YAML with its diff |
| `list_previews` | viewer | read-only | The branch / pull request previews of a git deployment |
| `promotion_plan` | viewer | read-only | What promoting a deployment to another would change (images, commit, diff) |
| `uptime_status` | viewer | read-only | Uptime checks with status, uptime 24 h / 7 d / 30 d, latency, certificate expiry, recent down periods |
| `cleanup_plan` | viewer | read-only | What can be removed from a host, by category, with sizes and the suggested picks |
| `list_schedules` | viewer | read-only | Scheduled start/stop (set in Kowan or by labels) and recent runs |
| `list_runbooks` | viewer | read-only | Runbooks of the workloads you can see (steps, params) |
| `list_backups` | viewer | read-only | Volume backup jobs (next run, last result) and recent runs |
| `container_action` | operator | idempotent: no | start / stop / restart / pause / unpause / kill |
| `deployment_action` | operator | open-world | deploy / redeploy (queued) / start / stop / sync (git) |
| `pull_image` | operator | idempotent, open-world | Pull with the stored registry credentials |
| `update_container` | operator | idempotent, open-world | Pull and recreate the container if a newer image exists (`force` to always) |
| `run_workflow` | operator | open-world | Manual workflow run |
| `workload_action` | operator | idempotent: no | start / stop / restart every container of a workload; a deployment starts and stops through Kowan |
| `rollback_deployment` | operator | idempotent: no | Restore a revision as a new one and redeploy (`deploy=false` to only restore) |
| `promote_deployment` | operator | idempotent: no | Apply the promotion plan to the target and redeploy it |
| `set_log_retention` | operator | idempotent | Keep a workload's logs for `days` (1–365), or `days=0` to stop and delete them |
| `run_uptime_check` | operator | idempotent, open-world | Probe a check now |
| `run_schedule` | operator | idempotent: no | Run a schedule's start/stop now |
| `run_runbook` | operator | idempotent: no | Run a runbook and wait (`confirm: true` when it asks); each step's output tail |
| `run_backup` | operator | open-world | Start a backup job now |
| `remove_container` | operator | **destructive** | Needs `confirm: true` |
| `remove_image` | operator | **destructive** | Needs `confirm: true` |
| `remove_volume` | operator | **destructive** | Needs `confirm: true` (the data is lost) |
| `prune` | operator | **destructive** | `kind`: containers / images (`all_images`) / volumes / networks; needs `confirm: true` |
| `destroy_deployment` | operator | **destructive** | Removes the running resources; `delete_record=true` also deletes the record; needs `confirm: true` |
| `create_deployment` | operator | | From compose YAML or a container spec, on one host; `deploy=true` deploys it immediately |
| `deploy_template` | operator | | Template id plus `inputs`; empty secret inputs get random values; deploys by default |
| `run_cleanup` | operator | **destructive** | `picks=suggested` removes what `cleanup_plan` suggests, `picks=listed` the given ids; needs `confirm: true` |
| `audit_log` | admin | read-only | Who did what: filter by `user`, `path`, `since` |

Every tool sets the MCP annotations `title`, `readOnlyHint`, `destructiveHint`,
`idempotentHint` and `openWorldHint`. `fleet_summary`, `list_containers` and `container_stats`
also declare an `outputSchema`. Every result includes `structuredContent` next to its text
block.

### Resources and prompts

- Resources (read-only JSON): `kowan://fleet/summary`, `kowan://hosts`, `kowan://alerts/open`,
  `kowan://deployments`, and the template `kowan://hosts/{host}/containers`.
- Prompts: `diagnose_container` (container, host), `fleet_health_report`, `update_review` (host).

## Security notes

- **Same credentials, same rules.** The endpoint uses Kowan's normal auth: an API key (`pk_...`)
  or a console login JWT, sent as a bearer token. The role works exactly as in the REST API:
  viewers get read-only tools, operators also get lifecycle, create and destructive
  tools (the destructive ones still need `confirm: true`), admins also get the audit
  log. Give an assistant a team-bound operator key rather than an admin key: admins
  bypass team grants.
- **Audit.** Every mutating tool call is written to the audit log with action `MCP`, path
  `/mcp/tools/<tool>`, the key or user as actor, and `source=mcp tool=... args=...` in the detail.
  Calls refused for lack of role are logged with status 403. In the logged args, compose files,
  specs and template input values are replaced by placeholders.
- **Destructive actions need `confirm: true`.** Without it the tool does nothing and asks the
  assistant to confirm with you first. Clients that honour `destructiveHint` will also ask before
  running these tools.
- **No secrets in outputs.** Inspect returns environment variable names only. Deployments hide
  env values, webhook tokens and git webhook secrets. Command lines are not returned.
- **Bounded outputs.** Logs default to 200 lines (max 2000), long lines are cut, lists are
  paginated (`limit`/`offset`), inspect lists are capped, and every text result is capped at
  64 KiB with a note saying what was cut.
- **Origin check (DNS rebinding).** Requests that carry an `Origin` header are accepted only from
  the same origin (the `Host` or `X-Forwarded-Host` the request was sent to), from
  `KOWAN_EXTERNAL_URL`, from an entry of `KOWAN_MCP_ALLOWED_ORIGINS` (comma-separated), or when
  both sides are loopback (the dev proxy). Anything else gets `403`. Clients that are not
  browsers send no `Origin` and are not affected.
- **Sessions** are random 256-bit ids, bound to the principal that created them. They are kept
  in memory with a 12 h idle timeout, so a server restart makes clients re-initialize.
- Treat an API key like a password, and give an assistant the lowest role that does the job.

## Protocol details

| Request | Response |
|---|---|
| `POST /mcp` with a JSON-RPC request | `200`, `application/json`, or a single SSE `message` event when the client accepts only `text/event-stream` |
| `POST /mcp` with a notification or a response | `202`, no body |
| `initialize` | `Mcp-Session-Id` response header; the version is negotiated (`2025-06-18`; `2025-11-25`, `2025-03-26` and `2024-11-05` are accepted) |
| Later requests without `Mcp-Session-Id` | `400` |
| Unknown, expired or someone else's session | `404` (re-initialize) |
| Unsupported `MCP-Protocol-Version` header | `400` |
| `GET /mcp` | `405` (no server-initiated stream) |
| `DELETE /mcp` | `204`; ends the session |
| Missing or invalid credentials | `401` with `WWW-Authenticate: Bearer` |
| Wrong `Content-Type` / `Accept` / `Origin` | `415` / `406` / `403` |

JSON-RPC errors: `-32700` parse error, `-32600` invalid request (batches are rejected, as in
`2025-06-18`), `-32601` unknown method, `-32602` invalid params or unknown tool, `-32003` role
not allowed, `-32002` unknown resource. Tool failures (Docker errors, bad arguments, missing
confirm) come back as a normal result with `isError: true` so the assistant can react.

Quick check with curl:

```bash
URL=https://kowan.example.com/mcp; KEY=pk_...
H=(-H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
SID=$(curl -si "${H[@]}" $URL -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
      | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
curl -s "${H[@]}" -H "Mcp-Session-Id: $SID" -H 'MCP-Protocol-Version: 2025-06-18' $URL \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fleet_summary"}}'
```

## Limitations

- JSON response mode only: no server-initiated SSE stream, no progress notifications, no
  `notifications/cancelled` handling. A long tool call (an image pull, for example) holds its
  HTTP request until it finishes.
- No OAuth authorization flow: clients must be able to send a static bearer header, or use the
  stdio bridge.
- Sessions live in memory, per server process.
- `container_logs` with `since` or `grep` searches only the last 2000 lines of a live
  container; `search_logs` goes further back, but only for workloads that keep their logs.
