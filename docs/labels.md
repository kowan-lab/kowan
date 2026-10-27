# Container labels

Kowan reads a few `kowan.*` labels from containers so you can configure behaviour
from a compose file without touching the UI. **Labels win over the values
stored in the UI/API**, field by field; the UI shows a `label` badge next to a
field that comes from a label and renders it read-only.

Precedence for every auto-update field: **label > stored policy (UI/API) > global default**.

| Label | Values | Default | Effect |
|---|---|---|---|
| `kowan.update` | `off` \| `notify` \| `pull` \| `apply` | `off` | Auto-update mode for this container (see below). |
| `kowan.update.window` | window spec | any time | Maintenance window for `apply`. |
| `kowan.update.min-age` | duration, e.g. `2d`, `36h`, `1d12h`, `1w` (bare number = days) | none | Only apply an image once it is at least this old. |
| `kowan.scan.block` | `never` \| `critical` \| `high` \| `worse` | global default (`never`) | Scan gate for updates of this container. |
| `kowan.notify` | `off` (also `false`, `0`, `no`) | on | No monitor alerts and no auto-update alerts for this container (history is still recorded). |

An invalid label value is ignored and reported on the container's
Auto-update panel. An invalid `kowan.update` falls back to `off` (never to a
more aggressive stored mode).

## Example

```yaml
services:
  web:
    image: nginx:1.27
    labels:
      kowan.update: apply
      kowan.update.window: "02:00-05:00 sat,sun"
      kowan.update.min-age: 2d
      kowan.scan.block: critical
  debug-tools:
    image: nicolaka/netshoot
    labels:
      kowan.notify: "off"
```

## Modes

- `off`: nothing happens automatically (the "update available" badge and
  the manual Update button keep working).
- `notify`: when the registry has a newer digest for the container's tag,
  raise an alert (bell + notification channels) once per new digest.
- `pull`: pull the new image in advance (once per digest), do not switch.
- `apply`: inside the window and once the image is old enough: pull, run the
  scan gate, recreate the container with the same configuration, verify it,
  and roll back automatically when verification fails.

Verification: the new container must be running, its healthcheck (if it
defines one) must report `healthy`, and its restart count must not increase,
for the "stable" period (default 10 s) within the deadline (default 120 s;
both in the Auto-update settings). On failure the container is recreated from
the previous image, pinned by its repo digest (`nginx@sha256:...`), because the
tag already points at the new image. Kowan remembers the original tag and the
failed digest: it keeps following the tag and does not retry that digest;
the next newer image is applied normally.

Containers that belong to a deployment with its own auto-update policy are
handled by that deployment's policy, unless the container sets `kowan.update`.

## Maintenance windows

Evaluated in the control plane's local time.

| Spec | Meaning |
|---|---|
| `02:00-05:00` | every day 02:00 to 05:00 |
| `02:00-05:00 sat,sun` | weekends (tokens in any order; `mon-fri`, `weekdays`, `weekends` work) |
| `mon-fri 22:00-06:00` | overnight; the night belongs to its start day |
| `cron 0 3 * * 6 2h` | opens at each cron match and stays open 2 h (default 1 h, max 24 h) |
| `0 3 * * 6` / `@daily 3h` | cron without the keyword, macros allowed |

## Minimum age

The age of a new image is the larger of "time since Kowan first saw the new
digest in the registry" and "time since the image was built" (its `Created`
time, once pulled and if plausible: reproducible builds stamp 1970 and are
ignored). With `min-age` set and nothing known yet, Kowan waits.

## Checks

A background loop checks every host every 6 h by default (configurable,
minimum 15 min), staggered per host. Remote digests come from the same cache
as the "update available" badge. Between checks, objects waiting for a window
or a minimum age are re-evaluated every minute without registry calls.
Everything is visible on the **Auto-update** page (policies, next window,
last result, paginated history).
