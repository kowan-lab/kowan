# Uptime checks and the status page

HTTP(S) and TCP checks the server runs on a schedule, with response times,
incidents, alerts through the notification channels, certificate expiry, and
an optional public status page.

Code: `kowan/uptime/` (`probe.py`, `runner.py`), `kowan/store/uptime.py`,
`kowan/api/uptime.py`; UI: `pages/Uptime.tsx`, `pages/StatusPage.tsx`
(public `/status`), `components/deployment/DeploymentUptime.tsx`.

## Checks

| field | meaning |
|---|---|
| `kind` | `http` (URL) or `tcp` (`host:port`) |
| `expectStatus` | accepted codes, e.g. `200-399` or `200,204` |
| `keyword` | text the body must contain (HTTP) |
| `intervalSec`, `timeoutSec` | how often, how long |
| `failThreshold` | failures in a row before it counts as down |
| `deploymentId` | links the check to a workload (shown on its overview) |
| `channelId`, `level` | where the alert goes, and how loud |
| `public` | listed on the status page |

Going down opens an incident and raises an alert (`ruleId uptime:<id>`)
through the bus, so channels hear it like any monitor alert; recovery closes
both and announces "Up again" at the same level. HTTPS certificates are read
every 6 hours. Results are kept 90 days.

## Status page

`GET /api/v1/public/status` (no session) and the page at `/status`: title,
description and the public checks with their state and 30 days of daily
uptime. Targets and error messages are never published. Turned on and
titled under Uptime → Status page.
