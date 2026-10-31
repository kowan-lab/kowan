---
title: AI assistants (MCP)
description: Connect Claude Code, Claude Desktop, Cursor and other MCP clients to Kowan, so AI assistants can inspect and operate your Docker hosts within an API key's role.
---

Kowan includes a [Model Context Protocol](https://modelcontextprotocol.io) server, so AI assistants such as Claude Code, Claude Desktop and Cursor can inspect and operate your Docker hosts. An assistant works with the permissions of the API key you give it, and every change it makes is written to the audit log. There is nothing extra to install on the server.

Ask, for example, "what's crashing on my servers?" or "show me the last errors of the api container on host nas".

## How it works

Endpoint
:   Your Kowan URL followed by `/mcp`, such as `https://kowan.example.com/mcp`.

Transport
:   Streamable HTTP.

Authentication
:   An API key, sent as `Authorization: Bearer pk_...`. A sign-in token works too.

Local bridge
:   `kowanctl mcp`, for clients that only start local processes.

**Settings > AI assistants** in the console shows your endpoint, ready-to-paste setups for Claude Code, Claude Desktop and Cursor, and the tools your role can call.

## Create an API key

Give each assistant its own [API key](api-keys.md), with the lowest role that does the job. An admin creates it in **Settings > API keys**, or from a terminal:

```bash
kowanctl apikey create claude --role viewer
```

The key's role decides which tools the assistant sees:

| Role | The assistant can |
|---|---|
| `viewer` | Look: hosts, containers, logs, deployments, alerts, uptime, schedules, backups, resource history. |
| `operator` | Also act: start, stop and restart, deploy, create and destroy deployments, roll back and promote, pull and update images, remove containers, images and volumes, prune, run a disk cleanup, and run workflows, schedules, backups and uptime checks. Destructive tools still ask for confirmation. |
| `admin` | Also read the audit log. |

A key bound to a team or to specific scopes only sees and acts on what those grants cover. For an assistant that should only look, a `viewer` key is the safe choice.

## Connect a client

In the examples, replace `https://kowan.example.com` with your Kowan URL and `pk_...` with the API key.

### Claude Code

Run in a terminal:

```bash
claude mcp add --transport http kowan https://kowan.example.com/mcp \
  --header "Authorization: Bearer pk_..."
```

### Claude Desktop

Claude Desktop starts MCP servers as local processes. [Install kowanctl](cli.md#install), then add Kowan to `claude_desktop_config.json`:

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

`kowanctl mcp` reads messages on standard input, forwards each one to your Kowan server and writes the replies on standard output. If the server restarts, it opens a new session on its own.

If Claude Desktop cannot find `kowanctl`, set `command` to its absolute path, such as the output of `which kowanctl`.

The bridge takes the server URL from `--url`, then `KOWAN_URL`, then the usual `kowanctl` configuration. It takes the credential from `--api-key`, then `KOWAN_API_KEY`, then the usual configuration, then your `kowanctl login` token. See [Command line](cli.md#mcp).

### Cursor and other clients

Any client that supports remote MCP servers over HTTP with custom headers (Cursor, Windsurf, VS Code and others) takes the URL and an `Authorization` header. For Cursor, in `~/.cursor/mcp.json`:

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

Clients that only start local processes use the `kowanctl mcp` setup shown for Claude Desktop.

## Safety

Same rules as the API
:   The assistant authenticates like any API client, and its role and grants apply exactly as in the [REST API](api.md). It only sees the tools its role allows, and Kowan checks the role again on every call.

Confirmation for destructive tools
:   Tools that delete something (marked **destructive** below) do nothing unless the call includes `confirm: true`. Without it, Kowan tells the assistant to confirm with you first. Clients that honor the MCP destructive hint also ask you before running them.

Audit log
:   Every call to a tool that changes something is written to the [audit log](audit-log.md) with the action `MCP`, the path `/mcp/tools/<tool>`, the key or user that made it, and the arguments. Compose files, container specs and template values are replaced by placeholders. Calls refused for lack of role are logged too.

No secrets in answers
:   Container inspection returns environment variable names, never their values. Deployments hide environment values and webhook secrets; a revision shows them only to a role that can operate the deployment.

Bounded answers
:   Logs return 200 lines by default and 2,000 at most, lists are paged, and every answer is capped at 64 KiB with a note saying what was cut.

!!! warning
    Treat an API key like a password. Anyone who has it can do everything its role allows, with or without an assistant.

## Tools

Every tool that takes a `host` accepts a host ID or name, and defaults to `local`.

### Fleet and workloads

| Tool | Role | What it does |
|---|---|---|
| `fleet_summary` | viewer | What the dashboard shows: hosts online, running containers, crashed or unhealthy containers, open alerts, reclaimable disk. The best first call. |
| `list_hosts` | viewer | Hosts with their live online status. |
| `get_host` | viewer | One host with its Docker Engine information. |
| `list_workloads` | viewer | Workloads as the console groups them (deployments, Compose stacks, lone containers), the ones that need attention first. |
| `get_workload` | viewer | One workload: its containers, its deployment, whether its logs are kept, its memory trend. |
| `workload_resources` | viewer | CPU and memory of a workload over the last 1 to 720 hours. |
| `memory_growth` | viewer | Workloads whose memory keeps climbing (a likely leak), with the hours left before the limit. |
| `workload_action` | operator | Start, stop or restart every container of a workload. |

### Containers

| Tool | Role | What it does |
|---|---|---|
| `list_containers` | viewer | Containers on a host, or on every online host. Filter by state or name. |
| `inspect_container` | viewer | Image, state, exit code, health, restarts, ports, mounts, networks and environment variable names. |
| `container_logs` | viewer | Recent log lines, with `tail`, `since` and a text filter. |
| `container_stats` | viewer | Current CPU, memory, network and disk I/O. |
| `container_action` | operator | Start, stop, restart, pause, unpause or kill a container. |
| `update_container` | operator | Pull the image and recreate the container when a newer one exists. |
| `remove_container` | operator | Remove a container. **Destructive.** |

### Images, volumes and networks

| Tool | Role | What it does |
|---|---|---|
| `list_images` | viewer | Images on a host, largest first. |
| `list_volumes` | viewer | Volumes on a host. |
| `list_networks` | viewer | Networks on a host. |
| `check_updates` | viewer | Which containers have a newer image in their registry. |
| `pull_image` | operator | Pull an image with the stored registry credentials. |
| `remove_image` | operator | Remove an image. **Destructive.** |
| `remove_volume` | operator | Remove a volume and all its data. **Destructive.** |
| `prune` | operator | Remove unused containers, images, volumes or networks. **Destructive.** |

### Deployments

| Tool | Role | What it does |
|---|---|---|
| `list_deployments` | viewer | Deployments with their status, target host and Git source. |
| `get_deployment` | viewer | One deployment with its configuration and the live status of each target. |
| `list_revisions` | viewer | A deployment's revisions: who changed what, when, and which one runs. |
| `get_revision` | viewer | One revision as YAML, with its diff from another. |
| `list_previews` | viewer | The branch and pull request previews of a Git deployment. |
| `promotion_plan` | viewer | What promoting a deployment to another would change. |
| `list_templates` | viewer | The app templates with the inputs they ask for. |
| `deployment_action` | operator | Deploy, redeploy, start, stop, or sync a Git deployment. |
| `rollback_deployment` | operator | Restore an earlier revision as a new one and redeploy it. |
| `promote_deployment` | operator | Apply the promotion plan to the target and redeploy it. |
| `create_deployment` | operator | Create a deployment from a Compose file or a container spec, and optionally deploy it. |
| `deploy_template` | operator | Create and deploy a deployment from an app template. |
| `destroy_deployment` | operator | Remove a deployment's containers, and optionally the deployment. **Destructive.** |

### Logs and events

| Tool | Role | What it does |
|---|---|---|
| `search_logs` | viewer | Full-text search of the [retained logs](logs.md), including containers recreated or removed since. |
| `recent_events` | viewer | Docker events on a host from the last hours (24 at most). |
| `set_log_retention` | operator | Keep a workload's logs for 1 to 365 days, or stop keeping them. |

### Monitoring and automation

| Tool | Role | What it does |
|---|---|---|
| `list_alerts` | viewer | Open, resolved or all alerts. |
| `uptime_status` | viewer | Uptime checks with their status, uptime, latency and certificate expiry. |
| `run_uptime_check` | operator | Probe an uptime check now. |
| `list_workflows` | viewer | Workflows, to use with `run_workflow`. |
| `run_workflow` | operator | Start a workflow run. |
| `list_schedules` | viewer | Scheduled starts and stops, with their next run and recent results. |
| `run_schedule` | operator | Run a schedule's start or stop now. |
| `crash_snapshots` | viewer | Containers that crashed, and what one looked like when it died: logs, configuration, environment (secrets hidden). |
| `list_runbooks` | viewer | The runbooks of the workloads you can see: what each runs, where, and its parameters. |
| `run_runbook` | operator | Run a runbook with its parameters and return each step's output. One that asks for confirmation needs `confirm: true`. |
| `list_backups` | viewer | Volume backup jobs and recent runs. |
| `run_backup` | operator | Start a backup job now. |

### Maintenance

| Tool | Role | What it does |
|---|---|---|
| `cleanup_plan` | viewer | What can be removed from a host to free disk space, with sizes and suggestions. Removes nothing. |
| `run_cleanup` | operator | Remove what the cleanup plan suggests, or the items you list. **Destructive.** |
| `audit_log` | admin | Who did what, from the console, the API, the CLI or MCP. |

### Resources and prompts

Clients that support MCP resources can read these as JSON: `kowan://fleet/summary`, `kowan://hosts`, `kowan://alerts/open`, `kowan://deployments` and `kowan://hosts/{host}/containers`.

Three prompts are ready to use: `diagnose_container` (a container and optional host), `fleet_health_report`, and `update_review` (an optional host).

## Limitations

- Sessions live in the server's memory and expire after 12 hours without use. After a server restart, clients start a new session.
- A long tool call, such as an image pull, holds its request until it finishes. The server sends no progress updates.
- There is no OAuth sign-in flow: the client must send the API key as a header, or use `kowanctl mcp`.
- A web page can call the endpoint only from Kowan's own origin, `KOWAN_EXTERNAL_URL`, or an origin listed in `KOWAN_MCP_ALLOWED_ORIGINS`. Clients that are not browsers are not affected. See [Configuration](configuration.md).
