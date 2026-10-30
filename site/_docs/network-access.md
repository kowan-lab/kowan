---
title: Network access
description: Restrict which IP addresses and networks can reach the Kowan console, API and edge agent endpoint, and trust your reverse proxy to report the real client IP.
---

Network access rules limit who can reach Kowan by IP address, on top of signing in. You set the outer limits in the server's environment, and admins can narrow them further from the console.

## What you can restrict

| Variable | Set on | Limits |
|---|---|---|
| `KOWAN_UI_ALLOW_CIDRS` | The server | Who can reach the web console, the REST API and the MCP endpoint for AI assistants. |
| `KOWAN_EDGE_ALLOW_CIDRS` | The server | Which addresses edge agents can connect from. |
| `KOWAN_AGENT_ALLOW_CIDRS` | An agent in direct mode | Who can connect to the agent: set it to the Kowan server's address. |
| `KOWAN_TRUSTED_PROXIES` | The server | Which reverse proxies may report the client's address. |
| `KOWAN_PROXY_HEADER` | The server | The header those proxies use: `X-Forwarded-For` (default) or `X-Real-IP`. |

Each list is a comma-separated set of IPv4 or IPv6 addresses and CIDR ranges. An empty list, the default, means no limit.

```yaml
services:
  server:
    image: ghcr.io/kowan-lab/kowan:latest
    environment:
      KOWAN_UI_ALLOW_CIDRS: "192.168.1.0/24,10.8.0.0/16"
      KOWAN_EDGE_ALLOW_CIDRS: "203.0.113.0/24"
```

A request from an address outside the list gets a `403` with the message "access from this address is not allowed". If an entry in the environment is not a valid address or CIDR, the server refuses to start, so a typo cannot leave it open.

Public webhooks (`/hooks/...`) and the `/health` check stay reachable from anywhere, because CI systems and Git hosts call them from addresses you do not control. Webhooks are protected by the secret token in their URL instead. See [Workflows and webhooks](workflows-and-webhooks.md).

## Narrow the lists from the console

1. Open **Settings → Network access**. The **Your connection** card shows the address Kowan sees for you.
2. Under **Allowlists**, enter one address or CIDR per line in **Web UI, API and AI assistants** and in **Edge agents**. Leave a list empty to add no limit.
3. Enter **Your password** and click **Save lists**.

The new lists apply from the next request. Edge agents that are already connected stay connected until they reconnect.

How the console lists work:

- **They only narrow.** When both the environment and the console set a list, an address must be on both. Kowan warns you about entries outside the environment's list, since they let nobody in. The **Set in the environment** card shows the environment's values, read-only.
- **You cannot lock yourself out.** Kowan refuses a **Web UI, API and AI assistants** list that leaves out the address you are saving from.
- **A stolen session is not enough.** Only admins can change the lists, and accounts with a password must enter it to save. Every change is recorded in the [audit log](audit-log.md).
- Each list holds at most 100 entries.

## Behind a reverse proxy

Behind a reverse proxy, every request reaches Kowan from the proxy's address. Until you tell Kowan to trust the proxy, the allowlists, the [audit log](audit-log.md) and the sign-in attempt limits all see that one address.

Set `KOWAN_TRUSTED_PROXIES` to your proxy's address:

```bash
-e KOWAN_TRUSTED_PROXIES=172.18.0.2 \
-e KOWAN_PROXY_HEADER=X-Forwarded-For
```

Kowan then reads the client's address from the header in `KOWAN_PROXY_HEADER`, only on requests that come from a trusted proxy. From anyone else the header is ignored, so a client cannot pretend to be another address. With `X-Forwarded-For`, Kowan reads the list from the nearest hop backwards and takes the first address that is not one of your trusted proxies.

!!! warning
    List only your own proxies. Any address in `KOWAN_TRUSTED_PROXIES` can claim to be any client.

Trusted proxies and the header can only be set in the environment: changing them from a browser would let a caller decide which address the server believes.

## Agents in direct mode

In direct mode the Kowan server connects to the agent. On the agent, set `KOWAN_AGENT_ALLOW_CIDRS` to the server's address, and the agent turns away everyone else before the connection is set up:

```bash
-e KOWAN_AGENT_ALLOW_CIDRS=198.51.100.10
```

The agent checks the address the connection comes from and reads no proxy header. Edge agents connect out to the server, so they are limited with `KOWAN_EDGE_ALLOW_CIDRS` on the server instead. See [Adding servers](adding-servers.md).
