---
title: Health and inventory
description: Kowan's own health page (databases, disk, host latency, background loops) and the inventory of what runs where, as CSV or JSON.
---

## Health

**Settings → Health** (admins) shows how Kowan itself is doing, refreshed every 10 seconds:

- **Server**: version, running since, the size of its databases and the free space in its data folder.
- **Hosts**: online or not, the round trip of a ping (through the agent's tunnel for a remote host), last seen, and whether crash snapshots follow it.
- **Background loops**: the monitor, auto-update, uptime checks and log retention, when each last ran and whether it is late (it missed three of its runs).
- **Work in flight**: runbooks running, images being pulled, open WebSockets.

At the top, **Needs a look** lists what is wrong: a late loop, an offline host, a disk almost full. The same data is at `GET /api/v1/system/health`.

## Inventory

**Workloads → More → Export inventory** downloads a CSV (or JSON) with one row per container on every host you can see: host and its groups, workload and its kind, service, container, image and image id, state, published ports, creation time and the deployment that owns it.

From the API: `GET /api/v1/inventory?format=csv` (or `json`), with `&group=production` for one host group. The JSON lists the hosts that could not be read as well.
