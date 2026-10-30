---
title: Image updates
description: See which containers run outdated images, update them in one click, and set auto-update policies with maintenance windows, scan gates and rollback.
---

Kowan compares the image each container runs with the one its registry serves and tells you when a newer image is available. You can update by hand, move a deployment to a newer version tag, or set an auto-update policy that pulls, verifies and rolls back on its own.

## Two kinds of update

| Question | Example | Where you see it | What changes |
| --- | --- | --- | --- |
| Was the image behind this tag pushed again? | `nginx:latest` or `api:2.0` rebuilt | **Update · N** in the workload header | The image only, with the same configuration |
| Does the registry have a newer version tag? | `api:2.0` to `api:2.1` | **New versions · N** in the workload header | The deployment's configuration, saved as a new revision |

## See which images are out of date

For every container, Kowan compares the digest of the image it runs with the digest the registry currently serves for the same tag. It asks the registry without pulling, using the credentials of a matching registry from **Settings → Registries** (anonymous otherwise). Answers are cached for 10 minutes.

The result shows up in several places:

- **Images**: the **Update** column reads **Update available**, **Up to date** or **Couldn't check**.
- A workload: an **Update · N** button in the header, and the count on the **Policies** tab.
- A container: an **Update** button in the header. **More → Check for image update** asks the registry again.

Images pinned by digest, and images built on the host that were never pushed, cannot be checked.

## Update a workload

1. Open the workload from **Workloads**.
2. Click **Update · N**. The dialog lists the services that have a newer image.
3. Click **Update**.

What Kowan does depends on the workload:

A Kowan deployment
:   Pulls the changed images, runs the [scan gate](#scan-gate), redeploys once and verifies every changed service. A service that does not come up healthy is rolled back to its previous image.

A Compose stack that Kowan does not manage
:   Recreates each container that has a newer image with its own configuration, and rolls it back if it does not come up.

A single container
:   Recreates the container on the new image with the same configuration.

!!! note
    The **Update** button of a container that belongs to a deployment updates the whole deployment. Recreating the container alone would be undone by the next deploy.

## Move to a newer version tag

For a deployment whose images use version tags, Kowan lists the newer tags of the same shape in the registry:

- A version tag is numbers separated by dots, with an optional `v` in front and an optional suffix: `1.27`, `v2.3.1`, `1.27-alpine`.
- Only tags with the same prefix, number of parts and suffix count: `1.27` is offered `1.28` but not `1.28.1`, and `1.27-alpine` stays on Alpine.
- `latest`, digests and images set from a variable are left out, with the reason shown.

To move to a newer tag:

1. Click **New versions · N** in the workload header.
2. Pick a new tag for each service, or keep the current one.
3. Review **What will change**: each service's old and new tag, and the diff of the deployment.
4. Keep the redeploy box ticked to deploy at once, then click **Apply**.

The change is saved as a new revision, so you can undo it from the **History** tab. See [Revisions and rollback](revisions-and-rollback.md). Deployments from a Git repository take their images from the repository: change the tag there.

## Automatic updates

An auto-update policy decides what Kowan does when it finds a newer image:

| Mode | Label value | What happens |
| --- | --- | --- |
| Off | `off` | Nothing automatic. The update badge and the **Update** button still work. |
| Notify | `notify` | An alert, once per new image. |
| Pull | `pull` | The new image is pulled in advance, once per new image. Nothing switches. |
| Apply (**Auto** on the workload) | `apply` | Inside the maintenance window and once the image is old enough: pull, scan gate, recreate with the same configuration, verify, and roll back on failure. |

### Set a policy in the console

1. Open the workload and go to the **Policies** tab.
2. Under **Updates**, set the mode with the knob: **Off**, **Notify**, **Pull** or **Auto**.
3. Click **Edit details** to set the **Maintenance window**, **Minimum image age** and **Scan gate**, then **Save policy**.

A single container has the same **Auto-update** section on its own page. **Check now** runs a check for that object, and **Reset to defaults** removes the stored policy.

A deployment's policy covers every service. A container that belongs to a deployment follows the deployment's policy, unless it has its own `kowan.update` label.

### Set a policy with labels

Labels let you keep the policy in your Compose file:

```yaml
services:
  web:
    image: nginx:1.27
    labels:
      kowan.update: apply
      kowan.update.window: "02:00-05:00 sat,sun"
      kowan.update.min-age: 2d
      kowan.scan.block: critical
```

`kowan.update` sets the mode. `kowan.update.window`, `kowan.update.min-age` and `kowan.scan.block` set the [maintenance window](#maintenance-windows), [minimum age](#minimum-image-age) and [scan gate](#scan-gate) described below. `kowan.notify: "off"` silences the container's alerts.

Labels win over values set in the console, field by field: the console shows a `label` badge and makes that field read-only. An invalid value is ignored and reported on the container's **Auto-update** panel, and an invalid `kowan.update` falls back to `off`. See [Container labels](container-labels.md) for every label Kowan reads.

### Maintenance windows

Windows use the Kowan server's local time. Without a window, updates may run at any time.

| Window | Meaning |
| --- | --- |
| `02:00-05:00` | Every day, 02:00 to 05:00 |
| `02:00-05:00 sat,sun` | Weekends only. `mon-fri`, `weekdays` and `weekends` work too. |
| `mon-fri 22:00-06:00` | Overnight. The night belongs to the day it starts. |
| `cron 0 3 * * 6 2h` | Opens at each cron match and stays open 2 hours (1 hour by default, 24 at most) |
| `@daily 3h` | Cron without the keyword; macros work |

### Minimum image age

A minimum age keeps Kowan from installing an image the moment it is published. Write it as a duration such as `2d`, `36h`, `1d12h` or `1w`; a bare number means days.

The age of a new image is the larger of the time since Kowan first saw it in the registry and the time since it was built (known once the image is pulled). Build dates that are not plausible, such as the 1970 date of reproducible builds, are ignored. While the age is unknown, Kowan waits.

### Verification and rollback

After switching, the new container must be running, report healthy if it has a health check, and keep the same restart count for 10 seconds, all within the verify deadline (120 seconds by default).

If verification fails, Kowan recreates the container from the previous image, pinned by digest (`nginx@sha256:...`) because the tag already points at the new one. It keeps following the tag, never retries the image that failed, and applies the next newer image normally.

!!! note
    **Update · N** on a deployment ignores the maintenance window and the minimum age, and retries a version that was rolled back before.

### Scan gate

A scan gate makes Kowan scan the new image with Trivy before switching, and block the update when the image has critical vulnerabilities (**Block on critical**), critical or high ones (**Block on critical or high**), or more of them than the current image (**Block if worse than current**). The default, **Never block**, runs no scan. The gate applies to the **Update** buttons and to `apply` policies. See [Vulnerability scanning](vulnerability-scanning.md#block-risky-updates) for the details.

## The Auto-update page

**Automation → Auto-update** lists every container and deployment with a policy, its window, gates and last result, and a history of every outcome (updated, rolled back, blocked, failed, notified, pulled), kept for 90 days. Operators can click **Check all now**.

Admins set the global behavior under **Settings** on the same page:

| Setting | Default | Notes |
| --- | --- | --- |
| Background checks | On | Turns the periodic check on or off |
| Check interval (hours) | 6 | 15 minutes at least. Hosts are staggered so they do not hit their registries at once. |
| Default scan gate | Never block | Used when neither a label nor a policy sets one |
| Health verify deadline (s) | 120 | From 15 to 1800 |

Between checks, objects waiting for their window or minimum age are re-evaluated every minute, without calling the registry.

## Alerts

Blocked, rolled-back and failed updates, and every **Notify** hit, raise an alert in the bell and on your [notification channels](alerts-and-notifications.md), unless the container has the label `kowan.notify=off`. The [digest](digest.md) also lists pending updates.

## Limits

- Swarm deployments are not auto-updated. The background updater handles standalone containers and Compose deployments.
- Updating the Kowan server and its agents is a separate process. See [Upgrading](upgrading.md).

## API

| Method and path | What it does |
| --- | --- |
| `GET /api/v1/hosts/{hostID}/updates` | Update status of every container on a host. `?refresh=1` skips the cache. |
| `POST /api/v1/deployments/{id}/update` | Update a deployment's images, as **Update · N** does |
| `GET`, `PUT`, `DELETE /api/v1/deployments/{id}/autoupdate` | Read, set or remove a deployment's policy |
| `GET`, `PUT`, `DELETE /api/v1/hosts/{hostID}/containers/{id}/autoupdate` | The same for a container |
| `POST /api/v1/autoupdate/check` | Run a check now |
| `GET /api/v1/deployments/{id}/tag-updates` | Newer version tags per service |

See [REST API](api.md) for authentication.
