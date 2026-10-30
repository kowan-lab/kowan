---
title: Disk cleanup
description: Free disk space on a Docker host: see what unused containers, images, volumes and build cache take, remove them safely, or clean up on a schedule.
---

Docker keeps stopped containers, old images, unused volumes and build cache until you remove them. Disk cleanup lists everything on a host that nothing uses, with its size, picks the items that are safe to remove, and can run the same cleanup every day or week.

## Open disk cleanup

Disk cleanup works one host at a time. Open it in either of two ways:

- Go to **Settings → Hosts & agents**, open the host and click **Disk cleanup**. The button shows while the host is online.
- On the **Overview** of a single host, click **Clean up** in the **Resources** card. The button shows when there is space to reclaim.

The top of the page shows how much space your picks free out of everything that can go, and how Docker's data on the host splits between images, containers, volumes and build cache.

## What Kowan lists

| Category | What it holds | Picked by default |
|---|---|---|
| Stopped containers | Containers that are not running. Removing one deletes its own writable layer; its image and volumes stay. | Yes, unless a deployment owns it |
| Untagged images | Leftovers of rebuilt or re-pulled images. Nothing can use them. | Yes |
| Unused images | Tagged images that no container uses. A deploy pulls them again when needed. | Only those 30 days old or older |
| Unused volumes | Volumes that no container mounts. | Anonymous volumes only |
| Build cache | Build cache not in use. It is rebuilt on the next image build. | Yes |

Some items are listed but left unpicked, with a note:

- A stopped container that belongs to a [deployment](deployments.md) was stopped on purpose, and the deployment's next deploy recreates it.
- A named volume may hold data that no container happens to use right now.
- An unused image newer than 30 days is likely to be needed again soon.

Sizes are estimates from Docker's own disk usage report, the one `docker system df` shows.

## Clean up a host

1. Open disk cleanup for the host.
2. Review each category. Check or clear single items, or use **All** and **None** for a whole category.
3. Click **Clean up**. The button shows the size you picked.
4. Confirm with **Remove**. If you picked a named volume, the dialog names it and warns that its data is deleted for good.

Kowan reads the disk usage again before it removes anything and skips items that are in use again or gone. When it finishes, a message says how much space was freed and which items were skipped.

!!! warning "Removing a volume deletes its data"
    There is no undo. Back up a named volume before you remove it; see [Volume backups](volume-backups.md).

Viewers can open the page and see the list. Running a cleanup and changing the schedule need the operator or admin role; see [Users, roles and teams](users-and-roles.md).

## Clean up on a schedule

The **On a schedule** card at the bottom of the page runs the safe part of the cleanup on its own. It never removes named volumes or a deployment's containers.

1. Turn on **Clean up this host on a schedule**.
2. Under **Every**, choose **Day** or **Week**. For a weekly run, choose the day under **On**.
3. Choose the hour under **At (UTC)**.
4. Choose what to remove (see the table below), then click **Save**.

| Option | Default | Removes |
|---|---|---|
| Stopped containers no deployment owns | On | Stopped containers, except a deployment's. |
| Untagged images | On | Every untagged image. |
| Anonymous volumes nothing mounts | Off | Unused volumes without a name. |
| Build cache | On | Build cache not in use. |
| Unused images older than (days) | 0 | Tagged images no container uses, at least this many days old. `0` keeps them all. |

Each host has its own schedule, which starts out off, weekly, on Sunday at 04:00 UTC. Kowan checks every 10 minutes whether a run is due, so it starts within the chosen hour. If the Kowan server is not running during that hour, the run waits for the next slot.

Each run posts a notice in the bell with the space it freed or the error it hit, and the card shows the last run.

## Get warned before the disk fills up

The **Docker disk usage** alert rule fires when Docker's data on a host crosses a size you set, in GB. See [Alert rule types](alerts-and-notifications.md#alert-rule-types).

## Use the API

```text
GET  /api/v1/hosts/{hostId}/cleanup            what can go, the schedule and the last scheduled run
POST /api/v1/hosts/{hostId}/cleanup            remove the items you list (operator)
PUT  /api/v1/hosts/{hostId}/cleanup/schedule   save the schedule (operator)
```

A cleanup run takes the ids to remove, as listed by the `GET`:

```json
{"containers": ["<id>"], "images": ["<id>"], "volumes": ["<name>"], "buildCache": true}
```

The schedule uses these fields, with `weekday` from 0 (Monday) to 6 (Sunday) and `hour` in UTC:

```json
{
  "enabled": true,
  "every": "weekly",
  "weekday": 6,
  "hour": 4,
  "containers": true,
  "dangling": true,
  "anonymousVolumes": false,
  "buildCache": true,
  "imagesOlderThanDays": 60
}
```

An AI assistant connected through the [MCP server](mcp.md) can also read the cleanup plan, and, for an admin and after you confirm, run it. See [REST API](api.md) for authentication.
