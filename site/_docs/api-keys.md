---
title: API keys
description: Create API keys for kowanctl, CI pipelines, scripts and AI assistants, limit each one to a role, a team or specific scopes, and revoke keys you no longer need.
---

An API key lets a script, a CI pipeline, `kowanctl` or an AI assistant call Kowan without signing in. Each key has a role, and you can narrow it further to a team's grants or to specific scopes. Only admins can see and manage API keys.

## Create a key

1. Open **Settings → API keys** and click **New API key**.
2. Enter a **Name** you will recognize, such as `ci-deploy`. Actions taken with the key appear in the [audit log](audit-log.md) under the user `key:<name>`.
3. Pick a **Role**: Admin, Operator or Viewer. The default is Viewer.
4. Choose the **Access**. See [Limit what a key can reach](#limit-what-a-key-can-reach).
5. Click **Create**, then click **Copy** and store the key somewhere safe.

!!! warning "Copy the key now"
    The dialog after **Create** is the only time Kowan shows the full key. Kowan stores only a hash of it, so it cannot show it again. If you lose a key, delete it and create a new one.

A key is `pk_` followed by 64 hexadecimal characters. The list in **Settings → API keys** shows its **Prefix** (the first 11 characters, such as `pk_1a2b3c4d`) to help you tell keys apart, along with when it was created and last used.

## Limit what a key can reach

| Access | The key can |
|---|---|
| **Everything the role allows** | Act with its role on every server, like a user in no team. |
| **Act as a team** | Act with the grants of the **Team** you pick, capped by its role. |
| **Only specific scopes** | Act only on the scopes you add, capped by its role. Each scope is a host, deployment, Compose stack or label selector with a level, as in [team grants](users-and-roles.md#teams-and-grants). |

A key limited to a team or to scopes cannot have the Admin role, since admins bypass every permission check.

!!! note "Deleting a team"
    A team that still has API keys bound to it cannot be deleted: Kowan names those keys so you can revoke them first. A key cannot be moved to another team.

Give each client its own key with the lowest role that does the job. A key does not expire: it works until you delete it.

## Use a key

Send the key in the `X-API-Key` header, or as a bearer token:

```bash
curl -H "X-API-Key: $KOWAN_API_KEY" https://kowan.example.com/api/v1/hosts
curl -H "Authorization: Bearer $KOWAN_API_KEY" https://kowan.example.com/api/v1/hosts
```

Browsers cannot set headers on WebSocket connections, such as live logs, so for those requests only Kowan also accepts the key in a `token` query parameter. Ordinary API requests never read credentials from the URL.

With `kowanctl`, pass `--api-key` or set the `KOWAN_API_KEY` environment variable:

```bash
export KOWAN_SERVER=https://kowan.example.com
export KOWAN_API_KEY=pk_...
kowanctl hosts ls
```

AI assistants connect to the MCP endpoint with an API key, and the key's role decides which tools they can use. See [AI assistants (MCP)](mcp.md) and [REST API](api.md).

## Manage keys from the command line

```bash
kowanctl apikey create ci-deploy --role operator   # prints the key once
kowanctl apikey ls
kowanctl apikey rm <id>
```

`kowanctl apikey create` sets the role only. To bind a key to a team or to scopes, use the console or send `teamId` or `scopes` to `POST /api/v1/apikeys`. See [Command line](cli.md).

## Revoke a key

In **Settings → API keys**, click the delete icon on the key's row and confirm. Every client still using the key stops authenticating at once.

You cannot edit a key. To change its role or access, create a new key, switch your clients to it, and delete the old one.
