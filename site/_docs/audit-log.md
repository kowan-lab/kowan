---
title: Audit log
description: See who changed what in Kowan and when: every change through the console, API, CLI or AI assistants, and every sign-in, with user, status and IP address.
---

The audit log records every change made in Kowan, whether through the web console, the REST API, `kowanctl` or an AI assistant, along with every sign-in attempt. Only admins can read it.

## Open the audit log

Click **Activity → Audit log** in the sidebar. It shows the latest 500 entries, newest first, and the search box filters them.

| Column | Shows |
|---|---|
| **Time** | When it happened. |
| **User** | Who did it. Actions taken with an [API key](api-keys.md) show as `key:<name>`. |
| **Action** | The HTTP method (`POST`, `PUT`, `PATCH`, `DELETE`), a sign-in event, or a named event such as `grant.create`. |
| **Path** | The API path that was called, which identifies the object that changed. |
| **Status** | The HTTP status of the response: a `2xx` succeeded, a `4xx` was refused, a `5xx` failed. |
| **IP** | The address the request came from. |

To see the history of one deployment, open it and click the **Activity** tab. It shows that deployment's entries in plain words, such as "Started" or "Deploy requested".

## What is recorded

- **Every change through the API:** each `POST`, `PUT`, `PATCH` or `DELETE` request, including those that were refused or failed. The web console and `kowanctl` both use the API, so their changes are covered. Reads are not recorded.
- **Sign-ins:** `login` and `login_sso` for successful sign-ins, `login_failed` for failed ones, and `login_throttled` when Kowan refuses an attempt after too many failures. A failed single sign-on shows the user `sso:<provider>`.
- **Named events** with details of what changed:
    - teams and grants: `team.create`, `team.update`, `team.delete`, `team.member.add`, `team.member.remove`, `grant.create`, `grant.delete`;
    - single sign-on: `sso.provider.create`, `sso.provider.update`, `sso.provider.delete`, `sso.settings.update`;
    - files edited inside containers: `container.files.write`, `container.files.upload`, `container.files.mkdir`, `container.files.rename`, `container.files.delete`.
- **AI assistant actions:** every tool call that changes something, even a refused one, with the action `MCP` and the path `/mcp/tools/<tool>`. The details list the arguments, with Compose files, specs and input values hidden. See [AI assistants (MCP)](mcp.md).

!!! tip "Behind a reverse proxy"
    Without `KOWAN_TRUSTED_PROXIES`, every entry shows your proxy's address instead of the client's. See [Network access](network-access.md#behind-a-reverse-proxy).

## Read the log from the API

`GET /api/v1/audit` returns `{"items": [...], "total": n}`, newest first. Each item also has a `detail` field that the console does not show, holding the details of named events and AI assistant calls.

| Parameter | Default | What it does |
|---|---|---|
| `limit` | `100` | Entries per page, at most `500`. |
| `offset` | `0` | Entries to skip, for paging. |
| `user` | None | Keep entries whose username contains this text. |
| `since` | None | Keep entries at or after this time, in RFC 3339 format. |
| `path` | None | Keep entries for this path and every path below it, such as `/api/v1/deployments/<id>`. |

```bash
curl -H "X-API-Key: $KOWAN_API_KEY" \
  "https://kowan.example.com/api/v1/audit?user=alice&since=2026-10-01T00:00:00Z&limit=200"
```

The key must have the Admin role. AI assistants using an admin key can read the log with the `audit_log` tool.

## Retention

Kowan keeps audit entries for 90 days and deletes older ones every hour. To change that, set `KOWAN_AUDIT_RETENTION` on the server to a duration such as `30d`, `180d` or `720h`. Set it to `0` to keep entries forever.

```bash
-e KOWAN_AUDIT_RETENTION=365d
```

The audit log lives in Kowan's database, so a [server backup](backup-and-restore.md) includes it. See [Configuration](configuration.md) for the other settings.
