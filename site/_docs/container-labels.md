---
title: Container labels
description: Reference of every kowan.* Docker label: configure auto-updates, scan gates, alerts, schedules and backups from a Compose file, and read the labels Kowan sets.
---

Kowan reads a few `kowan.*` labels from your containers, so you can configure updates, alerts, schedules and backups in a Compose file or a `docker run` command, without touching the UI. It also sets labels of its own on the containers and volumes it creates. This page lists both.

## Labels you set

Put them on a service in a Compose file, or pass them with `docker run --label`. Kowan reads them from the running container, so recreate the container after changing a label.

```yaml
services:
  web:
    image: nginx:1.27
    labels:
      kowan.update: apply
      kowan.update.window: "02:00-05:00 sat,sun"
      kowan.update.min-age: 2d
      kowan.scan.block: critical
  worker:
    image: ghcr.io/acme/worker:3.2
    labels:
      kowan.schedule.stop: "0 22 * * 1-5"
      kowan.schedule.start: "0 8 * * 1-5"
      kowan.schedule.timezone: Europe/Rome
      kowan.backup: "true"
  debug-tools:
    image: nicolaka/netshoot
    labels:
      kowan.notify: "off"
```

Quote values that YAML would otherwise read as a boolean or a number, such as `"off"` and `"true"`.

### Updates and alerts

| Label | Values | Default | Effect |
| --- | --- | --- | --- |
| `kowan.update` | `off`, `notify`, `pull`, `apply` | `off` | The auto-update mode of this container. See [Update modes](#update-modes). |
| `kowan.update.window` | A [maintenance window](#maintenance-windows) | Any time | When `apply` may update the container. |
| `kowan.update.min-age` | A duration: `2d`, `36h`, `1d12h`, `90m`, `1w`. A bare number means days. | None | Apply a new image only once it is at least this old. |
| `kowan.scan.block` | `never`, `critical`, `high`, `worse` | The global default (`never`) | The [vulnerability](vulnerability-scanning.md) gate for updates. See [Scan gate](#scan-gate). |
| `kowan.notify` | `off` (also `false`, `0`, `no`, `none`, `disabled`) | On | No monitoring alerts and no auto-update alerts for this container. Its history is still recorded. |

A label wins over the policy set in the UI or the API, field by field: **label, then stored policy, then global default**. In the UI, a field set by a label shows a `label` badge and is read-only.

An invalid value is ignored and reported on the container's auto-update panel. An invalid `kowan.update` falls back to `off`, never to a more aggressive mode stored in the UI.

The containers of a [deployment](deployments.md) that has its own auto-update policy follow that policy, unless the container sets `kowan.update` itself.

#### Update modes

`off`
:   Nothing happens on its own. The **Update** badge and button keep working.

`notify`
:   When the registry has a newer image for the container's tag, Kowan raises an alert (the bell and your [notification channels](alerts-and-notifications.md)) once per new image.

`pull`
:   Kowan pulls the new image in advance, once per new image, and does not switch to it.

`apply`
:   Shown as **Auto** in the UI. Inside the window, and once the image is old enough, Kowan pulls it, runs the scan gate, recreates the container with the same configuration, checks that it stays healthy, and rolls back automatically if it does not. See [Image updates](image-updates.md).

#### Maintenance windows

Windows use the Kowan server's local time.

| Value | Open |
| --- | --- |
| `02:00-05:00` | Every day from 02:00 to 05:00 |
| `02:00-05:00 sat,sun` | Weekends only. Tokens can come in any order; `mon-fri`, `weekdays` and `weekends` also work. |
| `mon-fri 22:00-06:00` | Overnight. The night belongs to the day it starts. |
| `cron 0 3 * * 6 2h` | From each cron match, for 2 hours (default 1 hour, at most 24 hours) |
| `0 3 * * 6` or `@daily 3h` | The same without the `cron` keyword; macros such as `@daily` work |

#### Scan gate

| Value | Blocks an update when the new image has |
| --- | --- |
| `never` | Never blocks |
| `critical` | Any critical vulnerability |
| `high` | Any critical or high vulnerability |
| `worse` | More critical, or more high, vulnerabilities than the image it replaces |

When the new image cannot be scanned, every value except `never` blocks the update.

### Schedules

| Label | Values | Effect |
| --- | --- | --- |
| `kowan.schedule.start` | A cron expression (5 fields, or a macro such as `@daily`) | Start the container at these times. |
| `kowan.schedule.stop` | A cron expression | Stop the container at these times. |
| `kowan.schedule.restart` | A cron expression | Restart the container at these times. |
| `kowan.schedule.timezone` | An IANA time zone, such as `Europe/Rome` | The time zone of the three labels above. Default: the server's local time. |

Kowan looks for these labels on every host every 5 minutes. They appear on the **Schedules** page as read-only schedules. An invalid cron expression or time zone shows the schedule with its error, and it never runs. A schedule you create in Kowan for the same host, container and action overrides the label, even when it is disabled: disabling it is how you silence a label without touching the container. See [Schedules](schedules.md).

### Runbooks

| Label | Values | Effect |
| --- | --- | --- |
| `kowan.runbook.<name>.command` | A shell command | Declares the runbook `<name>` on this service (required). |
| `kowan.runbook.<name>.title`, `.description` | Text | How it is shown. |
| `kowan.runbook.<name>.mode` | `exec` (default) or `run` | In a running container, or in a throwaway copy of the service. |
| `kowan.runbook.<name>.replicas` | `one` (default) or `all` | On one replica or on each, in turn. |
| `kowan.runbook.<name>.confirm` | `true` | Asks for confirmation before running. |
| `kowan.runbook.<name>.params` | `NAME, OTHER=default` | Parameters, read by the command as `$NAME`. |
| `kowan.runbook.<name>.timeout` | Seconds | Default 600. |

They appear in the workload's **Runbooks** tab, read-only. See [Runbooks](runbooks.md).

### Crash snapshots and group schedules

| Label | Values | Effect |
| --- | --- | --- |
| `kowan.snapshot` | `"off"` | No [crash snapshot](crash-snapshots.md) for this container. |
| `kowan.schedule.group` | `"off"` | A [host group](host-groups.md) schedule leaves this container alone. |

### Backups

| Label | Values | Effect |
| --- | --- | --- |
| `kowan.backup` | `true` (also `1`, `yes`, `on`) | Includes the volume, or the volumes mounted by the container, in every backup job that picks volumes by label. |

Set it on a container or directly on a volume. The volumes are resolved again at each run. See [Volume backups](volume-backups.md).

## Labels Kowan sets

Kowan writes these labels on what it creates, and uses them to find its own resources. Do not set or change them by hand.

| Label | Set on | Meaning |
| --- | --- | --- |
| `kowan.managed` | Containers of container deployments | `true`: created by a Kowan deployment. |
| `kowan.deployment.id` | Containers of container deployments | The ID of the deployment that owns the container. |
| `kowan.deployment.name` | Containers of container deployments | The name of that deployment. |
| `kowan.exposure.domain` | Containers published on [Exposure](exposure-and-https.md) | The domain. |
| `kowan.exposure.proxy` | Published containers | The proxy that routes the domain. |
| `kowan.exposure.router` | Published containers | The Traefik router Kowan added. |
| `kowan.exposure.network` | Published containers | The proxy network Kowan connected the container to. |
| `kowan.exposure.keys` | Published containers | The exact labels Kowan added, so **Unexpose** removes only those and keeps the ones you wrote. |
| `kowan.exposure.role` | The Traefik container Kowan deploys | `proxy`. |
| `kowan.registry.role` | A [registry Kowan deployed](registries.md#deploy-your-own-registry) | `registry` on its container, `config` on the volume holding its password file. |
| `kowan.support` | Kowan's helper containers | `true` on vulnerability scan containers, `volume-browse` on the helper that browses a volume. Containers with `true` are skipped by auto-update. |
| `kowan.scan` | Vulnerability scan containers | `trivy`. |
| `kowan.volbackup.helper` | Volume backup helper containers | Marks a helper that runs a backup task. |
| `kowan.role` | Helper containers that update the Kowan server or an agent | For example `server-updater` or `agent-updater`. |
| `kowan.update.id` | The same update helpers | The update they belong to. |
| `kowan.restored-from` | Volumes restored from a backup | The first 16 characters of the snapshot ID. |

Compose deployments are recognized by their Compose project name (the `com.docker.compose.project` label), not by `kowan.deployment.id`. See [Importing stacks](importing.md).

Containers labeled `kowan.support=true`, `kowan.role` or `kowan.volbackup.helper`, like the Kowan server and agents themselves, are hidden from Workloads, the dashboard, Logs and search unless an admin turns on **Kowan's own containers** in **Settings**.

Kowan also reads one label it never sets: `kowan.component=agent` helps it find a host's agent container when it updates the agent, if that container has neither the usual name nor the official agent image.
