---
title: Resource history
description: See what uses each host, a workload's CPU, memory and network over 6 hours to 30 days, each container's share, and get warned when memory keeps climbing without a restart.
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

When the workload has more than one container, **Per container** below the charts draws one line per container, for memory or CPU, so a replica or a service that weighs more than the others stands out. A swarm task is shown as its service and slot, such as `shop_web.2`, so a replaced task continues the same line. The 8 biggest are drawn. Per-container history is kept 7 days: on the **30d** range it covers the last week.

## See what uses a host

The **Overview** of a single host has a **What uses this host** card below its resources. Choose **Memory** or **CPU** and a range.

- The chart stacks the biggest workloads, each in its own colour, and puts the rest together as **Other**. Hover it to read each workload's value at that time.
- The table lists every workload that ran on the host in the range, biggest first, with its value now, its average over the range and its peak. A workload that did not run for part of the range counts as 0 there, so averages compare like for like. Open a workload from its name.

Users limited by a team see only the workloads they can read.

## How Kowan records it

Kowan samples the stats of every running container on every host every 15 seconds by default. It folds the samples into one point per workload and host every 5 minutes: average and peak CPU, average and peak memory, the memory limit, the network counters and the number of containers. Ranges longer than 2 days are shown as hourly points.

Each container also gets its own point, without network figures. History is kept 30 days, per container 7 days. Only running containers are sampled, so a stopped workload leaves a gap, and a new workload has no history until its first points are recorded.

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

Two settings in **Settings → Data retention** control them: **Monitoring interval** (15s by default) is how often Kowan samples container stats and checks alert rules, and **Resource history** (7d by default) is how long the host samples are kept. Changes apply at once.

The 30-day workload history and the 5-minute points are fixed. See [Configuration](configuration.md).

## Use the API

```text
GET /api/v1/resource-history/{workload}?hours=24
```

```text
GET /api/v1/resource-history/{workload}/containers?hours=24
GET /api/v1/resource-history/host/{host-id}?hours=24&top=6
```

`hours` goes from 1 to 720 (30 days). The first response holds `points`, each with `time`, `containers`, `cpu`, `cpuMax`, `mem`, `memMax`, `memLimit`, `netRx` and `netTx`, and `growth`, the current memory trend. A workload id has the form `dep~<deployment-id>`, `stack~<host-id>~<project>` or `ctr~<host-id>~<container-name>`, as at the end of the workload's page address.

The second returns `members`, one per container, each with `member`, `hostId` and `points` (`time`, `cpu`, `cpuMax`, `mem`, `memMax`). The third returns `times`, `workloads` (each with `workload`, `cpu`, `cpuMax`, `cpuNow`, `mem`, `memMax`, `memNow` and `containers`), `series` for the `top` biggest by memory and by CPU, and `other`, the sum of the rest. Users limited by a team need read access to the workload's containers or its deployment.

An AI assistant connected through the [MCP server](mcp.md) can read a workload's history and list the workloads whose memory keeps growing. See [REST API](api.md) for authentication.
