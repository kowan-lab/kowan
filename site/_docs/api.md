---
title: REST API
description: Authenticate to the Kowan REST API with an API key or sign-in token, and find the endpoints for hosts, containers, deployments, webhooks and more.
---

Everything the Kowan web console does goes through its REST API, so you can script any of it. The API is served by your Kowan server under `/api/v1`, speaks JSON, and applies the same roles and permissions as the console.

```bash
curl -H "X-API-Key: $KOWAN_API_KEY" https://kowan.example.com/api/v1/hosts
```

```json
[
  {
    "id": "local",
    "name": "local",
    "type": "local",
    "mode": "agent",
    "address": "",
    "online": true,
    "lastSeen": "2026-10-30T22:13:44Z",
    "createdAt": "2026-10-30T22:13:43Z"
  }
]
```

## Authentication

Every endpoint under `/api/v1` needs a credential, except the few [public endpoints](#public-endpoints). Use an API key for scripts and integrations, or a sign-in token to act as a user.

### API keys

An admin creates API keys in **Settings > API keys**, or with `kowanctl apikey create`. A key has a role, and can be limited to a team or to specific hosts. Its secret starts with `pk_` and is shown only once. See [API keys](api-keys.md).

Send the key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: pk_..." https://kowan.example.com/api/v1/deployments
```

The `Authorization` header works too, as `Authorization: Bearer pk_...`.

### Sign-in tokens

Sign in with a username and password to get a token for that user:

```bash
curl -X POST https://kowan.example.com/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "..."}'
```

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "user": {
    "id": "d0d2c837-4fa4-4da4-ba7d-9fdda549442a",
    "username": "alice",
    "role": "operator",
    "email": "",
    "authSource": "local",
    "createdAt": "2026-10-30T22:13:43Z"
  }
}
```

Send the token as `Authorization: Bearer <token>`. It is valid for 24 hours. `POST /api/v1/auth/logout` revokes it earlier. Changing the user's password or role, or deleting the user, also ends it.

Too many failed sign-ins return `429` with a `Retry-After` header. In SSO-only mode, only the break-glass account can sign in with a password: use an API key instead. See [Single sign-on](single-sign-on.md).

### WebSocket connections

Live logs, container terminals, live stats and Docker events are WebSocket endpoints. Browsers cannot set headers on a WebSocket handshake, so these also accept the credential as a `token` query parameter: `wss://kowan.example.com/api/v1/hosts/local/containers/web/logs?token=pk_...`. Plain REST requests never read credentials from the URL.

## Roles

The role of the user or API key decides what it can call. Admins can call everything.

| Role | Can |
|---|---|
| `viewer` | Read: hosts, containers, images, volumes, networks, deployments, workflows, alerts, uptime checks, logs. |
| `operator` | Also act: start, stop and remove containers, pull images, create and deploy deployments, run workflows, manage alert rules, uptime checks, schedules and backup jobs. |
| `admin` | Also administer: hosts, users, teams, API keys, credentials and registries, single sign-on, GitOps, the audit log, server backups and updates. |

A user who belongs to a team, or an API key bound to a team or to scopes, only reaches what its grants cover, never more than its role. Lists leave out what it cannot see. See [Users, roles and teams](users-and-roles.md).

## Requests and responses

- Request and response bodies are JSON. Send `Content-Type: application/json`. A JSON request body can be up to 1 MiB.
- Field names are camelCase: `createdAt`, `hostId`, `lastSeen`.
- Times are RFC 3339 timestamps in UTC, such as `2026-10-30T22:13:44Z`.
- Objects Kowan stores, such as deployments, workflows and users, have UUIDs. The server Kowan runs on is the host with ID `local`. Containers, images, volumes and networks keep their Docker IDs and names.
- Endpoints may reject a field they do not know with a `400`, so send only the fields you mean to set.
- Long operations, such as a deploy, are queued: the request returns `202` and the work continues in the background.

### Errors

An error has an HTTP status and a JSON body with an `error` message:

```json
{"error": "host not found"}
```

| Status | Meaning |
|---|---|
| `400` | The request is invalid: a missing field, a bad value, a Compose file with errors. |
| `401` | The credential is missing, invalid or expired. |
| `403` | Your role or permissions do not allow it, or your address is not allowed by the [network access](network-access.md) rules. |
| `404` | The object does not exist, or its feature is turned off in **Settings**. |
| `409` | The request conflicts with the current state: a name already in use, deletion protection, a deploy already running. |
| `429` | Too many sign-in attempts. Wait for the `Retry-After` seconds. |
| `502` | The host or its agent could not be reached, or Docker returned an error. |

## Endpoints

Paths below are relative to `/api/v1`. `{hostId}` is a host ID and `{id}` the ID of the object. Every change you make through the API is recorded in the [audit log](audit-log.md).

### Account

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/login` | Sign in with `username` and `password`. Public. |
| `POST` | `/auth/logout` | Revoke the token you send. |
| `GET` | `/me` | The signed-in user. |
| `GET` | `/me/permissions` | Your role, teams and grants. |

### Hosts

Any role can read hosts. Adding, renaming and removing them needs an admin.

| Method | Path | Description |
|---|---|---|
| `GET` | `/hosts` | List hosts with their online status. |
| `POST` | `/hosts` | Add a host: `{"name": "nas", "mode": "edge"}`, or `"mode": "agent"` with an `address`. Returns the host and the `docker run` command for its agent. |
| `GET` | `/hosts/{hostId}` | One host. |
| `PATCH` | `/hosts/{hostId}` | Rename a host: `{"name": "..."}`. |
| `DELETE` | `/hosts/{hostId}` | Remove a host. |
| `GET` | `/hosts/{hostId}/edge-command` | The agent's `docker run` command again. Admin only. |
| `GET` | `/hosts/{hostId}/system` | Docker Engine information. |
| `GET` | `/hosts/{hostId}/df` | Disk usage of images, containers, volumes and build cache. |
| `GET` | `/hosts/{hostId}/metrics` | Recorded CPU, memory and container counts. |
| `GET` | `/hosts/{hostId}/events` | Recent Docker events. `/events/ws` streams them live. |

See [Adding servers](adding-servers.md).

### Containers

Listing, inspecting, logs and stats need any role. Actions, terminals, reading or changing files, and removal need an operator.

| Method | Path | Description |
|---|---|---|
| `GET` | `/hosts/{hostId}/containers` | List running containers. `?all=true` includes stopped ones. |
| `POST` | `/hosts/{hostId}/containers` | Create a container. |
| `GET` | `/hosts/{hostId}/containers/{id}` | Inspect a container. |
| `POST` | `/hosts/{hostId}/containers/{id}/start` | Start it. Also `stop`, `restart`, `pause`, `unpause` and `kill`. |
| `DELETE` | `/hosts/{hostId}/containers/{id}` | Remove it. `?force=true` removes a running container. |
| `POST` | `/hosts/{hostId}/containers/{id}/update` | Pull its image again and recreate it with the same configuration. |
| `GET` | `/hosts/{hostId}/containers/{id}/logs` | WebSocket: stream its logs. `tail` sets the backlog (500 lines by default), `follow=false` sends the backlog and closes. |
| `GET` | `/hosts/{hostId}/containers/{id}/stats` | One sample of CPU, memory, network and disk I/O. `/stats/stream` is a live WebSocket. |
| `POST` | `/hosts/{hostId}/containers/{id}/exec` | Run a command, `{"cmd": ["ls", "/"]}`, and return its output. `GET` on the same path opens an interactive WebSocket terminal. |
| `GET` | `/hosts/{hostId}/containers/{id}/files` | Browse the container's files. Sub-paths read, write, upload, download, rename and delete. |

```bash
curl -X POST -H "X-API-Key: $KOWAN_API_KEY" \
  https://kowan.example.com/api/v1/hosts/local/containers/web/restart
```

### Images, volumes and networks

Listing and inspecting need any role. Changes, and browsing volume files, need an operator.

| Method | Path | Description |
|---|---|---|
| `GET` | `/hosts/{hostId}/images` | List images. |
| `POST` | `/hosts/{hostId}/images/pull` | Pull an image, with the stored registry credentials. |
| `DELETE` | `/hosts/{hostId}/images/{id}` | Remove an image. |
| `POST` | `/hosts/{hostId}/build` | Build an image. |
| `GET` | `/hosts/{hostId}/updates` | Which containers have a newer image. `?refresh=1` asks the registries again. |
| `POST` | `/hosts/{hostId}/images/{id}/scan` | Scan an image for vulnerabilities. `GET` on the same path returns the last report. |
| `GET` | `/hosts/{hostId}/volumes` | List volumes. `POST` creates one. |
| `DELETE` | `/hosts/{hostId}/volumes/{name}` | Remove a volume and its data. |
| `GET` | `/hosts/{hostId}/volumes/{name}/browse` | Browse a volume's files. `download`, `archive` and `upload` move files in and out. |
| `GET` | `/hosts/{hostId}/networks` | List networks. `POST` creates one. |
| `POST` | `/hosts/{hostId}/networks/{id}/connect` | Connect a container to a network. `disconnect` does the opposite. |
| `DELETE` | `/hosts/{hostId}/networks/{id}` | Remove a network. |
| `POST` | `/hosts/{hostId}/prune/{kind}` | Remove unused `containers`, `images`, `volumes` or `networks`. For images, `?all=true` removes every unused image, not only untagged ones. |
| `GET` | `/hosts/{hostId}/cleanup` | What a [disk cleanup](disk-cleanup.md) can remove, with sizes. `POST` runs it. |

### Deployments

Reading needs any role. Creating, changing and running deployments needs an operator. See [Deployments](deployments.md).

| Method | Path | Description |
|---|---|---|
| `GET` | `/deployments` | List deployments. |
| `POST` | `/deployments` | Create a deployment. It is saved, not deployed. |
| `GET` | `/deployments/{id}` | One deployment. |
| `PUT` | `/deployments/{id}` | Change it. A change to what it runs saves a new [revision](revisions-and-rollback.md). |
| `DELETE` | `/deployments/{id}` | Remove its containers and delete it. |
| `POST` | `/deployments/{id}/deploy` | Deploy in the background (`202`). Also `redeploy`, `start`, `stop`, `destroy`, and `sync` for Git deployments. |
| `GET` | `/deployments/{id}/status` | The state of each target. |
| `GET` | `/deployments/{id}/revisions` | Its revisions. `/revisions/{n}/diff` compares two, `POST /revisions/{n}/rollback` restores one. |
| `GET` | `/deployments/{id}/previews` | Its branch previews. `POST` with `{"branch": "..."}` creates one. |
| `GET` | `/deployments/{id}/promote` | What [promoting](previews-and-promotion.md) it would change. `POST` promotes it. |
| `POST` | `/deployments/{id}/webhook` | Create or rotate its redeploy webhook. Returns the `url`. |
| `POST` | `/compose/validate` | Check a Compose file before you save it. |
| `GET` | `/templates` | The app templates. `POST /templates/{id}/render` fills one in. |

A container deployment on the local host, then its deploy:

```bash
curl -X POST https://kowan.example.com/api/v1/deployments \
  -H "X-API-Key: $KOWAN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "whoami", "type": "container", "strategy": "standalone",
       "targets": ["local"], "spec": {"image": "traefik/whoami:latest"}}'

curl -X POST -H "X-API-Key: $KOWAN_API_KEY" \
  https://kowan.example.com/api/v1/deployments/<id>/deploy
```

### Automation

Reading needs any role, except workflow runs. Creating, changing and running need an operator.

| Method | Path | Description |
|---|---|---|
| `GET` | `/workflows` | List workflows. `POST` creates one. |
| `POST` | `/workflows/{id}/run` | Start a run. |
| `GET` | `/workflows/{id}/runs` | Its run history. |
| `POST` | `/workflows/{id}/webhook` | Create or rotate its webhook. |
| `GET` | `/schedules` | Scheduled starts and stops. `POST /schedules/{id}/run` runs one now. |
| `GET` | `/volbackups/jobs` | [Volume backup](volume-backups.md) jobs. `POST /volbackups/jobs/{id}/run` runs one now. |
| `GET` | `/autoupdate/overview` | [Image update](image-updates.md) policies and their state. |
| `GET` | `/gitops/syncs` | [GitOps](gitops.md) syncs. Admin only. |

See [Workflows and webhooks](workflows-and-webhooks.md) and [Schedules](schedules.md).

### Monitoring

Reading needs any role. Changes need an operator.

| Method | Path | Description |
|---|---|---|
| `GET` | `/alerts` | Alerts. `POST /alerts/{id}/resolve` resolves one. |
| `GET` | `/alert-rules` | Alert rules. `POST` creates one. |
| `GET` | `/uptime/checks` | [Uptime checks](uptime.md) with their status. `POST /uptime/checks/{id}/run` probes one now. |
| `GET` | `/log-archive/search` | Search [retained logs](logs.md): `q`, `workload`, `host`, `since`, `until`. |
| `PUT` | `/log-archive/retention/{workload}` | Keep a workload's logs: `{"days": 14}`. |
| `GET` | `/resource-history/{workload}` | CPU and memory of a workload over time. `?hours=` from 1 to 720. |
| `GET` | `/notifications` | Your notifications. |

A `{workload}` is written as the console writes it: `dep~<deployment id>`, `stack~<host id>~<compose project>` or `ctr~<host id>~<container name>`.

### Administration

These need an admin.

| Method | Path | Description |
|---|---|---|
| `GET` | `/users` | Users. `POST` creates one. |
| `GET` | `/teams` | Teams with their members and grants. `/grants` manages grants. |
| `GET` | `/apikeys` | API keys, without their secrets. `POST` creates one, `DELETE /apikeys/{id}` revokes it. |
| `GET` | `/credentials` | Registries, secrets and notification channels, with their sensitive values masked. |
| `GET` | `/sso/providers` | Single sign-on providers. |
| `GET` | `/audit` | The audit log. |
| `GET` | `/settings/network` | The network access lists. |
| `GET` | `/features` | Which optional features are on. Any role can read it. |
| `GET` | `/backup` | Download a backup of the server as `.tar.gz`. |
| `POST` | `/restore` | Upload a backup to restore at the next restart. |

## Public endpoints

These need no credential.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `ok` while the server is up. For load balancers and monitoring. |
| `POST` | `/hooks/deploy/{token}` | Redeploy a deployment. |
| `POST` | `/hooks/workflow/{token}` | Run a workflow. Its trigger can accept another method. |
| `POST` | `/hooks/gitops/{token}` | Sync a GitOps source after a push. |
| `GET` | `/api/v1/public/status` | The data of the public status page, when it is turned on. |
| `GET` | `/api/v1/auth/status` | Whether the server has its first admin yet. |

`/health` and the `/hooks/` paths are not limited by the web console allowlist (`KOWAN_UI_ALLOW_CIDRS`), since CI systems and Git hosts call them from addresses you do not control.

### Webhooks

The token in a webhook URL is its secret: anyone who has the URL can call it. Rotate it from the console or the API if it leaks. An unknown token returns `404`.

`/hooks/deploy/{token}`
:   Queues a redeploy and returns `202` with `{"status": "redeploying", "deployment": "<id>"}`. On a Git deployment, the same URL accepts push webhooks from GitHub, Gitea and GitLab. Kowan checks their signature, then redeploys only when auto-deploy is on and the push is to the deployment's branch. When the payload lists the changed files, at least one must be in the Compose file's folder. Pushes to other branches feed its [previews](previews-and-promotion.md).

`/hooks/workflow/{token}`
:   Starts a run and returns `202` with `{"runId": "<id>", "workflow": "<id>"}`. The webhook trigger decides which HTTP method it accepts (`POST` by default). The request body, headers and query parameters reach the workflow's steps.

```bash
curl -X POST https://kowan.example.com/hooks/deploy/3f9c...
```

See [Workflows and webhooks](workflows-and-webhooks.md) and [GitOps](gitops.md).

## Related

- [Command line](cli.md): `kowanctl` wraps the most common calls.
- [AI assistants (MCP)](mcp.md): the same permissions, as tools for AI assistants.
