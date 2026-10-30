---
title: Schedules
description: Start, stop, restart or redeploy containers, deployments and Compose stacks on a cron schedule, from the console or with kowan.schedule labels.
---

Schedules start, stop or restart containers, deployments and Compose stacks at set times, for example to stop a development stack at 22:00 on weekdays and start it again at 08:00. You set them in the console or with labels in your Compose files.

## What you can schedule

| Target | Identified by | Actions |
| --- | --- | --- |
| Container | Its name on a host, so the schedule survives a recreate | `start`, `stop`, `restart` |
| Deployment | The Kowan [deployment](deployments.md) | `start`, `stop`, `restart`, `deploy` |
| Stack | The Compose project name on a host | `start`, `stop`, `restart` |

`deploy` redeploys the deployment with its saved configuration. `restart` of a deployment stops it, then starts it.

## Create a schedule

1. Go to **Automation → Schedules** and click **New schedule**.
2. Pick the **Target type**, the **Host** (not needed for a deployment) and the **Target**.
3. Pick the **Action**. The **Name** is optional.
4. Enter the **Cron expression** and pick a **Timezone**. **Server local time** is the default. Presets such as **Weekdays 08:00** fill in common expressions, and the next three runs are shown as you type.
5. For a container stop or restart, turn on **Only if no exec session is open** to skip the run while someone has a console open in the container. This is best effort: a session that has just ended can still cause one skip.
6. Leave **Enabled** on and save.

You can also schedule from where you work:

- On a workload, the **Policies** tab has a **Schedule** section. Its knob switches between **Always on** and **Timer**: with no schedule yet, **Timer** opens a new one; otherwise the knob enables or disables the workload's schedules. **Manage schedules** lists them.
- On a container's page, the **Schedules** section has **Add schedule**.
- When you create a deployment, **Schedule** under **After creating** sets start and stop times.

### Cron syntax

An expression has five fields: minute, hour, day of month, month and day of week (0 is Sunday). Macros such as `@hourly`, `@daily`, `@weekly`, `@monthly` and `@yearly` work too.

| Expression | Runs |
| --- | --- |
| `0 22 * * 1-5` | At 22:00, Monday to Friday |
| `0 8 * * 1-5` | At 08:00, Monday to Friday |
| `0 4 * * 0` | At 04:00 on Sundays |
| `0 3 * * *` | Every day at 03:00 |

## Schedule with labels

Put the schedule on the container itself:

```yaml
services:
  app:
    image: ghcr.io/example/app:1.4
    labels:
      kowan.schedule.stop: "0 22 * * 1-5"
      kowan.schedule.start: "0 8 * * 1-5"
      kowan.schedule.restart: "0 4 * * 0"
      kowan.schedule.timezone: Europe/Rome
```

| Label | Value |
| --- | --- |
| `kowan.schedule.start` | Cron expression for starting the container |
| `kowan.schedule.stop` | Cron expression for stopping it |
| `kowan.schedule.restart` | Cron expression for restarting it |
| `kowan.schedule.timezone` | Optional time zone, such as `Europe/Rome` |

Kowan looks for these labels on every host every 5 minutes. Click **Rescan labels** on the Schedules page to pick up a change at once. Label schedules appear with a `label` badge and are read-only. One with an invalid cron expression or time zone is shown with its error and never runs.

A schedule you create for the same host, container and action overrides the label, whether it is enabled or not. To silence a label without touching the container, create a schedule for the same action and disable it. See [Container labels](container-labels.md) for every label Kowan reads.

## When a run is missed or skipped

- A run more than 10 minutes late, because the server was down for example, is not run late. It is recorded as skipped with how late it was, and the schedule moves on to its next time.
- A run whose previous run is still going is skipped.
- After a restart, Kowan works out the next run from the last recorded one, so a run missed during the downtime is reported.

Daylight saving time is handled for you. A time that does not exist on the day clocks go forward runs once, at the first valid minute after the gap. A time that happens twice when clocks go back runs once, the first time.

## Run history

**Run now** starts a schedule's action immediately, including a label schedule. The **Run history** section of the Schedules page lists every run with its trigger, result and message, kept for 90 days. A failed run posts a warning to the bell.

Anyone can see schedules; creating, editing and running them needs the operator role. See [Users and roles](users-and-roles.md).

## API

| Method and path | What it does |
| --- | --- |
| `GET /api/v1/schedules` | List schedules, filtered by `targetType`, `targetId` and `hostId` |
| `POST /api/v1/schedules` | Create a schedule |
| `GET`, `PUT`, `DELETE /api/v1/schedules/{id}` | Read, change or delete one |
| `POST /api/v1/schedules/{id}/run` | Run it now |
| `GET /api/v1/schedules/runs` | Run history, filtered by `scheduleId` |
| `POST /api/v1/schedules/preview` | The next runs of a cron expression in a time zone |
| `POST /api/v1/schedules/rescan` | Read the labels again |

To run maintenance tasks on a schedule instead, such as a script, an HTTP call or a prune, use a [workflow](workflows-and-webhooks.md) with a cron trigger.
