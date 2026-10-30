---
title: Uptime checks
description: Monitor websites, APIs and TCP ports with uptime checks, get alerted when they go down, track response times and certificates, and publish a status page.
---

Uptime checks probe a URL or a TCP port on a schedule. When a check fails several times in a row, Kowan opens an incident and alerts you, then tells you again when it recovers. You can publish chosen checks on a public status page.

## Create a check

1. Go to **Activity → Uptime** and click **New check**.
2. Choose **HTTP(S)** or **TCP port**.
3. Enter a **Name** and the target: a **URL** such as `https://shop.example.com/health`, or a **Host and port** such as `db.internal:5432`.
4. Adjust the options below and click **Create check**.

The check runs right away, then on its interval. Creating, editing and deleting checks needs the operator or admin role.

The Kowan server runs every check itself, not the agents, so the target must be reachable from the server. If a URL contains a user name and password, viewers see it without them.

### Check options

| Option | Default | Meaning |
|---|---|---|
| Method | GET | `GET` or `HEAD`. HTTP only. |
| Expected status | `200-399` | Status codes that count as up: ranges and single codes separated by commas, such as `200-299,301`. Redirects are followed. |
| Page contains | Empty | Text the response must contain, in its first 1 MB. HTTP only. |
| Interval | Every minute | Every 30 seconds, minute, 5 minutes, 15 minutes or hour. |
| Timeout (s) | 10 | From 1 to 60 seconds, and shorter than the interval. |
| Down after failures | 2 | Failures in a row before the check counts as down, from 1 to 10. |
| Notify | Every enabled channel | Or a single [notification channel](alerts-and-notifications.md#set-up-a-notification-channel). |
| Alert level | Critical | Critical, Warning or Info. |
| Workload | None | A [deployment](deployments.md) that shows the check in an **Uptime** card on its page. |
| Verify TLS certificate | On | Turn it off for a self-signed certificate. HTTP only. |
| Show on the status page | Off | Lists the check on the [public status page](#publish-a-status-page). |
| Enabled | On | Off pauses the check. |

## Read the results

The list shows, for each check:

- its status: **Up**, **Down**, **Pending** (not checked yet) or **Paused**,
- its most recent probes, as green and red bars,
- its uptime over the last 24 hours and 30 days, the share of probes that passed,
- its average response time over the last 24 hours,
- for HTTPS, the days left on the certificate, in amber under 21 days and red under 7.

Click a check's name to see its uptime over 24 hours, 7 days and 30 days, a response time chart for the last hour, 24 hours or 7 days, and its incidents with their start, duration and error. Operators and admins can click **Check now** to probe a check on the spot.

## Alerts and incidents

When a check reaches its failure threshold, Kowan opens an incident and raises the alert "Down: *name*" with the error, at the check's alert level. The first probe that passes closes the incident and sends "Up again: *name*" at the same level, so the channels that heard about the outage hear about the recovery too. Both go to the check's channel, or to every enabled channel whose minimum level allows them. See [Alerts and notifications](alerts-and-notifications.md).

Pausing a check while it is down closes its incident and resolves its alert. Deleting a check deletes its history and resolves its open alert.

## Certificate expiry

For HTTPS checks, Kowan reads the certificate every 6 hours and shows how many days it has left, even when the certificate does not validate. Certificates that expire within 21 days are also listed in the [digest](digest.md).

## Publish a status page

Admins can publish a public page at `/status` on your Kowan address with the checks marked **Show on the status page**.

1. In **Activity → Uptime**, find the **Status page** card.
2. Turn on **Public status page**.
3. Enter a **Title** (up to 80 characters; "Service status" when blank) and an optional **Description** (up to 500).
4. Click **Save**. The card then links to the page.

The page shows an overall state, each public check with its current state and 30 days of daily uptime, and recent incidents. It never shows targets or error messages, and leaves paused checks out. Anyone who can reach your Kowan server can open it without signing in.

!!! note
    The Kowan server runs the checks and serves the status page: while it is down, neither works.

## Limits and retention

- Kowan keeps probe results for 90 days.
- Up to 16 checks probe at the same time.
- Check names must be unique.

## Use the API

```text
GET    /api/v1/uptime/checks                checks with their uptime and response times
POST   /api/v1/uptime/checks                create a check (operator)
PUT    /api/v1/uptime/checks/{id}           change a check (operator)
DELETE /api/v1/uptime/checks/{id}           delete a check and its history (operator)
POST   /api/v1/uptime/checks/{id}/run       probe now (operator)
GET    /api/v1/uptime/checks/{id}/results   probes of the last ?hours=24
GET    /api/v1/uptime/incidents             outages, newest first (?check=<id>)
GET    /api/v1/public/status                the status page data, without signing in
```

A check in JSON, with `kind` set to `http` or `tcp`:

```json
{
  "name": "Shop website",
  "kind": "http",
  "target": "https://shop.example.com/health",
  "expectStatus": "200-399",
  "keyword": "ok",
  "intervalSec": 60,
  "timeoutSec": 10,
  "failThreshold": 2,
  "level": "critical",
  "public": true
}
```

Through the API, `intervalSec` goes from 10 seconds to a day. An AI assistant connected through the [MCP server](mcp.md) can read the checks and their outages and, as an operator, run a check. See [REST API](api.md) for authentication.
