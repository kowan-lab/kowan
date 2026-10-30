# Vulnerability scanning

Kowan scans images with [Trivy](https://trivy.dev), run as a short-lived
container **on the host that holds the image**, through that host's Docker
connection. It therefore works the same on the local daemon and on remote
agents (over the tunnel); nothing is installed on the control plane.

## How a scan runs

```
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v kowan-trivy-cache:/cache \
  aquasec/trivy:0.75.0 image --format json --scanners vuln --quiet \
    --image-src docker --cache-dir /cache --timeout 15m <image>
```

- the host's docker socket is mounted **read-only**; `--image-src docker`
  reads the image from that daemon (no registry pull, local images work);
- the named volume `kowan-trivy-cache` keeps Trivy's vulnerability DB, so it
  is downloaded once per host and re-used (the first scan takes longer);
- the scanner image is pinned (`aquasec/trivy:0.75.0`); override with
  `KOWAN_TRIVY_IMAGE`. `KOWAN_TRIVY_CACHE_VOLUME` and
  `KOWAN_TRIVY_CONTAINER_PREFIX` (default `kowan-trivy-`) rename the volume and
  the scan containers;
- scan containers carry `kowan.support=true` (hidden from the Containers list)
  and are removed when the scan ends; one scan runs at a time per host.

The JSON report is read from the container's stdout, folded into severity
counts and a list of findings (CVE, package, installed, fixed in, severity),
de-duplicated per (CVE, package, version), sorted most severe first and capped
at 1000 per image (counts stay exact).

## Storage and re-use

Results are stored per image digest (the local image id) in the `image_scans`
table: severity counts, top findings, scanner version, `scanned_at`, duration.
A successful result younger than 24 h is re-used for the same digest (also
from another host). A failed re-scan never overwrites a good result.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/hosts/{hostID}/images/{id}/scan` | latest result (with findings) + `running` |
| POST | `/api/v1/hosts/{hostID}/images/{id}/scan` | operator; start a scan in the background (`{"force": true}` re-scans a fresh result, `{"wait": true}` waits) |
| GET | `/api/v1/hosts/{hostID}/scans` | results for every image on the host + `containers` (containerId to imageId) + totals |

`{id}` is an image id (`sha256:...`) or a ref without `/`; for refs with `/`
pass `_` as id and `?image=<ref>`.

## UI

- Images list and container rows: compact severity chips (`C 2`, `H 5`, ...;
  a green `clean` when nothing was found).
- Image detail: "Vulnerabilities" band with counts, scanner version, a
  searchable, paginated findings table and **Scan now** / **Re-scan**.
- Container detail: chips in the header.

## Safe update gate

When a container is updated (manual **Update** button or auto-update
`apply`), the **new** image can be scanned before the switch. The policy comes
from the request, else the `kowan.scan.block` label, else the stored policy,
else the global default (Auto-update settings, default `never`):

| Policy | Blocks when |
|---|---|
| `never` | never (no scan is run for the gate) |
| `critical` | the new image has any CRITICAL finding |
| `high` | the new image has any CRITICAL or HIGH finding |
| `worse` | the new image has more CRITICAL, or more HIGH, findings than the current image (without a current scan to compare, only CRITICAL blocks) |

If the new image cannot be scanned, the gate fails closed (blocks) for every
policy except `never`. A blocked update leaves the running container
untouched, answers HTTP 409 on the manual route, is recorded in the
auto-update history and raises a warning alert (bell + channels) unless the
container has `kowan.notify=off`.
