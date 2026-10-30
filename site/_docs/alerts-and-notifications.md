---
title: Alerts and notifications
description: Create alert rules for CPU, memory, disk, crashes and unreachable hosts, and get alerts by email, Slack, Discord, Telegram, ntfy or webhook.
---

Kowan watches every host and container and raises an alert when something needs you: a container crashes, CPU stays high, a host stops answering. You see alerts in the web console and receive them on notification channels such as email, Slack or Telegram.

## Where alerts show up

**Activity → Alerts**
:   The 100 most recent alerts, newest first, with their level, rule, resource, host and status. Operators and admins resolve an open alert with the check mark on its row.

The bell in the top bar
:   A feed of alerts plus events that do not deserve a message of their own, such as a host going offline or coming back, an alert resolving itself, or a scheduled [disk cleanup](disk-cleanup.md). Use **Mark all read** to clear it. Items are kept 30 days.

Notification channels
:   Email, chat and webhook destinations. Each channel receives the alerts at or above its minimum level.

## Set up a notification channel

Only admins manage channels.

1. Go to **Settings → Notifications** and click **New channel**.
2. Enter a **Name** and choose the **Minimum level**: Info, Warning or Critical. The channel receives only alerts at or above it. New channels start at Warning.
3. Select the credential that holds the destination, or click **New credential** to create one: pick its **Type** and fill in the fields listed below.
4. Leave **Enabled** on and click **Create**.
5. Click **Send a test message** (the send icon on the channel's row) to check that it arrives.

A credential is defined once and can back several channels; all of them are listed under **Settings → Credentials**. Kowan stores credentials encrypted and masks their secret fields (passwords, tokens, Slack and Discord webhook URLs) when you view them.

### Channel types

| Type | Fields | What Kowan sends |
|---|---|---|
| Email (SMTP) | Host, From, To; optional Port, Username, Password | A plain-text email with the subject `[kowan][LEVEL] title`. The port defaults to 25. With a username, Kowan tries STARTTLS and signs in. Separate several recipients with commas. |
| Webhook | URL; optional Headers | A `POST` with the JSON body `{"title": "...", "body": "...", "level": "..."}`. Write headers as `Authorization:Bearer x;X-Env:prod`. |
| ntfy | Topic; optional Server, Token | A message on the topic, with the alert's title and a priority from its level: urgent for critical, high for warning, default for info. The server defaults to `https://ntfy.sh`. |
| Telegram | Bot Token, Chat ID | A message from your bot to the chat. |
| Slack | Webhook URL | A message through a Slack incoming webhook. |
| Discord | Webhook URL | A message through a Discord webhook. |

An alert message carries the alert's title and text, followed by the host's id (when the alert concerns a host) and the time.

## Create an alert rule

1. Go to **Activity → Alerts**, open the **Alert rules** tab and click **New rule**.
2. Enter a **Name** and choose the **Condition type**.
3. For a numeric type, set the **Operator** and the **Threshold**.
4. Choose the **Severity** (or keep **Type default**) and, under **Sustained for**, how long the condition must hold before the rule fires: immediately, or 1, 5, 10 or 30 minutes.
5. Choose the **Host**, or **All hosts**. For container rules, you can also set a **Container name filter** (part of the name) and a label selector (**Label key**, and **Label value** or blank for any value).
6. Under **Notification channel**, keep **All enabled channels** or pick one.
7. Click **Create**.

New rules are enabled and auto-resolve. Switch a rule off from its row in the list. Creating, editing and deleting rules needs the operator or admin role; see [Users, roles and teams](users-and-roles.md).

### Alert rule types

| Condition type | Fires when | Default |
|---|---|---|
| CPU usage | A running container's CPU crosses the threshold, as a share of the whole host: 100% means every core is busy. | Above 80%, warning |
| Memory usage | A running container's memory crosses the threshold, as a percentage of the memory limit Docker reports for it. | Above 80%, warning |
| Docker disk usage | The disk Docker uses on a host (images, containers, volumes and build cache) crosses the threshold, in GB. | Above 20 GB, warning |
| Restart loop | A running container's restart count crosses the threshold. | 5 restarts or more, warning |
| Container down | A container Kowan saw running is no longer running. | Critical |
| Container unhealthy | A running container's Docker healthcheck reports unhealthy. | Critical |
| Abnormal exit | A container that was running exited with a non-zero code, including one killed for running out of memory. | Critical |
| Host unreachable | A host's Docker endpoint stops answering. | Critical |

### How rules are evaluated

Kowan checks every enabled rule on every host every 15 seconds by default. A rule raises one alert per container (or per host) when its condition becomes true, and stays quiet while the condition holds. With **Auto-resolve** on, the alert is marked resolved when the condition clears and the bell shows a "Resolved" notice; channels get no message for it. With **Auto-resolve** off, the alert stays open until someone resolves it.

**Container down** fires for any container that stops, including one you stop yourself. **Abnormal exit** fires only for crashes. A container with the restart policy `always` or `unless-stopped` that stays exited was stopped on purpose, because Docker would have restarted it after a crash, so it raises no alert. With the policy `no`, or `on-failure` once its retries run out, a non-zero exit still counts.

Both types track the containers Kowan has seen running since the server last started: a container that was already stopped before then raises nothing.

## Alerts without a rule

These alerts need no rule. They go to every enabled channel that accepts their level, except uptime checks, which can name one channel.

| Source | Alerts | Level |
|---|---|---|
| [Uptime checks](uptime.md) | "Down", and "Up again" on recovery | The check's level, critical by default |
| [Memory growth](resource-history.md#memory-growth-warning) | "Memory keeps growing", and "Memory stable again" | Warning |
| [Image updates](image-updates.md) | Update available, pulled, blocked, rolled back or failed | Info to critical |
| [Volume backups](volume-backups.md) | Volume backup failed | Critical |

## Silence a container

Give a container the label `kowan.notify=off` to keep it out of alert rules and image-update alerts. Its history is still recorded. See [Container labels](container-labels.md).

```yaml
services:
  worker:
    image: example/worker
    labels:
      kowan.notify: "off"
```

## Use the API

```text
GET    /api/v1/alerts?limit=100        recent alerts, newest first
POST   /api/v1/alerts/{id}/resolve     resolve an alert (operator)
GET    /api/v1/alert-rule-types        rule types with their defaults
GET    /api/v1/alert-rules             list the rules
POST   /api/v1/alert-rules             create a rule (operator)
PUT    /api/v1/alert-rules/{id}        change a rule (operator)
DELETE /api/v1/alert-rules/{id}        delete a rule (operator)
```

The rule types are `cpu_high`, `mem_high`, `disk_high`, `container_restart_loop`, `container_down`, `container_unhealthy`, `container_exited` and `host_down`. `forDuration` is in seconds, and an empty `hostId` or `channelId` means all hosts or all enabled channels:

```json
{
  "name": "High CPU on web",
  "type": "cpu_high",
  "operator": ">",
  "threshold": 80,
  "severity": "warning",
  "forDuration": 300,
  "hostId": "",
  "match": "web",
  "channelId": "",
  "autoResolve": true,
  "enabled": true
}
```

See [REST API](api.md) for authentication.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `KOWAN_MONITOR_INTERVAL` | `15s` | How often Kowan checks the rules and samples container stats. |
| `KOWAN_ALERT_RETENTION` | `30d` | Resolved alerts older than this are deleted; open alerts stay. `0` keeps them all. |

See [Configuration](configuration.md) for every setting.
