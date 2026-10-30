---
title: Resource history
description: See a workload's CPU, memory and network over 6 hours to 30 days, and get warned when its memory keeps climbing without a restart, a likely leak.
---

Kowan records CPU, memory and network for every running workload and keeps 30 days of history. When a workload's memory climbs steadily without a restart, a likely memory leak, Kowan warns you before it runs out.

## View a workload's history

1. Go to **Infrastructure → Workloads** and open a workload.
2. On the **Overview** tab, find the **Resources** card and choose a range: **6h**, **24h**, **7d** or **30d**.

A workload is a deployment, a Compose stack or a lone container; see [Applications](applications.md). The card shows the latest value of each figure with its chart:

**CPU**
:   Average and peak, as a share of the whole host: 100% means every core is busy. A workload that runs on several hosts shows the hosts' average and the busiest host's peak.

**Memory**
:   Average and peak, added up over the workload's containers, with its memory limit. The chart is in MiB.

**Network**
:   Traffic in and out, per second. The chart is in KiB/s.

## How Kowan records it

Kowan samples the stats of every running container on every host every 15 seconds by default. It folds the samples into one point per workload and host every 5 minutes: average and peak CPU, average and peak memory, the memory limit, the network counters and the number of containers. Ranges longer than 2 days are shown as hourly points.

History is kept 30 days. Only running containers are sampled, so a stopped workload leaves a gap, and a new workload has no history until its first points are recorded.

## Memory growth warning

Each time a 5-minute point is recorded, Kowan fits a straight line through the workload's memory over the last 6 hours, on each host. It counts as a steady climb when all of these hold:

- there are at least 24 points, spanning at least 2 hours,
- the line fits the points well (R² of 0.8 or more),
- memory rose by at least 50 MiB and by at least 20% of where it started,
- the number of containers did not change, since a restart or a scale resets memory.

When they hold, the **Resources** card shows **Memory keeps growing** with the rise, the pace per hour and, when the workload has a memory limit, about how many hours are left before it reaches it. Kowan also raises a warning alert, "Memory keeps growing: *name*", sent to every enabled [notification channel](alerts-and-notifications.md#set-up-a-notification-channel) that accepts warnings. When the climb stops, the alert resolves and the channels receive "Memory stable again".

The warning needs no setup. To be alerted at a fixed level instead, create a **Memory usage** [alert rule](alerts-and-notifications.md#create-an-alert-rule).

## Host charts

The **Overview** of a single host shows the host's CPU and memory over the last hour. These come from separate samples, one per host at the same interval, kept 7 days by default.

| Variable | Default | Effect |
|---|---|---|
| `KOWAN_MONITOR_INTERVAL` | `15s` | How often Kowan samples container stats and checks alert rules. |
| `KOWAN_METRICS_RETENTION` | `168h` | How long the host samples are kept (7 days). |

The 30-day workload history and the 5-minute points are fixed. See [Configuration](configuration.md).

## Use the API

```text
GET /api/v1/resource-history/{workload}?hours=24
```

`hours` goes from 1 to 720 (30 days). The response holds `points`, each with `time`, `containers`, `cpu`, `cpuMax`, `mem`, `memMax`, `memLimit`, `netRx` and `netTx`, and `growth`, the current memory trend. A workload id has the form `dep~<deployment-id>`, `stack~<host-id>~<project>` or `ctr~<host-id>~<container-name>`, as at the end of the workload's page address. Users limited by a team need read access to the workload's containers or its deployment.

An AI assistant connected through the [MCP server](mcp.md) can read a workload's history and list the workloads whose memory keeps growing. See [REST API](api.md) for authentication.
