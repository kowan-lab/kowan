# Self-update and rollback (server and agents)

Status: implemented, 2026-10-25 (`kowan/updater/`). Refinements made while
building it are folded in below and summarised in "Implementation notes". A
transactional container swap that can always roll back.

## Goals

- Update the control plane (running as a container) and every agent without
  manual steps, using only the Docker socket already mounted. No extra volumes,
  no external orchestrator: the product updates itself.
- Never lose a working installation. Any failure before the new version is
  proven healthy restores the previous container (and, for the server, the
  database) automatically.
- One-click manual rollback to the previous version after a "successful"
  update that later turns out bad.
- Every update leaves a readable record: phases, timings, logs on failure.

## Non-goals

Running from source (no container) keeps reporting `supported: false` with a
clear reason. Swarm services and Kubernetes are out of scope.

## Vocabulary

- **target**: the container being updated (`kowan-server` or `kowan-agent`, found
  as today by `KOWAN_SERVER_CONTAINER` / HOSTNAME / name / image repo).
- **helper**: a one-shot container that performs the swap. It runs from the
  **current** (known-good) image of the target, not the new one, so a broken new
  image can never break the tool that has to roll it back.
- **parked**: the old target, stopped and renamed `<name>-previous`, kept until
  the next successful update so manual rollback is instant.

## Flow

### 1. Orchestrator (server process; for agents it drives the agent host over the tunnel)

1. **Lock**: one update per component at a time (row in `updates` with status
   `running`; stale after 30 min).
2. **Resolve** the target image ref. Default: the running ref; when it is
   pinned to a release older than the newest one (`kowan:0.1.3` with 0.1.4
   out), that release's tag instead (`kowan:0.1.4`), so a pinned server does
   not pull the same version again. Status reports it as `updateImage`.
   Optional explicit `image` (pin a version, e.g. `ghcr.io/kowan-lab/kowan:1.4.2`).
3. **Pull** the image on the target daemon (registry auth from stored registries).
   Record digest. Verify the image OS/arch matches the daemon.
4. **Server only: DB snapshot** into `<data>/backups/pre-update-<id>.db` using the
   sqlite online backup API (consistent while running, checked with
   `PRAGMA quick_check`) plus copies of `secret.key` / `jwt.key`. Keep the last 3
   snapshots (never the one a manual rollback would need). Taken BEFORE the
   preflight so the preflight can migrate a throwaway copy of it.
5. **Preflight smoke test** (update mode only): run a throwaway container from
   the NEW image with entrypoint `python -m kowan.<server|agent> preflight`, the
   target's user env and its data mounts read-only (exits 0 when imports, config
   parsing, the image's own self-update helper and, for the server, migrations
   on a copy of the snapshot succeed; see below). Timeout 60 s. Failure aborts
   the update before anything is touched (status `aborted`).
6. **Launch helper** from the target's current image id with:
   - `command: self-update --plan <base64 json>`
   - the Docker socket, and for the server the same bind mounts/volumes as the
     target (so it can restore the DB snapshot on rollback),
   - `restart: no`, labels `kowan.role=<server|agent>-updater`,
     `kowan.update.id=<id>`, name `kowan-<server|agent>-updater-<id>`.
7. The POST responds `202 {id}` right after step 1 (lock); steps 2-6 run in the
   background and are recorded as phases. The UI polls `GET /system/updates/{id}`.

### 2. Helper (container-agnostic, `kowan/updater/helper.py`)

Phases, each logged as one JSON line on stdout
(`{"ts","id","phase","ok","detail"}`), final line `{"result": ...}`:

1. `wait` (default 3 s) so the orchestrator's response flushes.
2. `inspect` the target; capture full Config/HostConfig/networks.
3. `stop` the target (timeout from plan, default 20 s).
4. `park`: rename target to `<name>-previous` (an older `<name>-previous` is
   renamed `<name>-retired-<id8>` and only removed at commit) and set its
   restart policy to `no` via `POST /containers/{id}/update` (remember the
   original policy).
5. `create` the new container with the original name, cloned config, new image,
   label `kowan.update.id=<id>`; reconnect extra networks; `start`.
6. `verify` (default deadline 120 s):
   - container `Running` and `RestartCount` unchanged for 10 s, and
   - if the image defines a HEALTHCHECK: `State.Health.Status == healthy`
     (unhealthy or deadline = failure).
   Images ship a HEALTHCHECK: server `python -m kowan.server healthcheck`
   (GET `/health` on the local port), agent `python -m kowan.agent healthcheck`
   (fresh heartbeat file written while the tunnel session is up in edge mode or
   the listener is serving in direct mode).
7. On success: `commit`. Keep `<name>-previous` (stopped, restart `no`) for
   manual rollback. Exit 0, final `{"result":"updated", ...}`.
8. On any failure after `park`: `rollback`:
   - capture the last 200 log lines of the new container into the result,
   - stop the new container, set its restart policy to `no` and rename it
     `<name>-failed-<id8>` (it is removed only after the old one is healthy),
   - server only, if the new container was started: restore the DB snapshot
     (the current `kowan.db`/`-wal`/`-shm` are moved to
     `backups/rolled-back-<id>-<label>/`, never deleted; the `updates` table is
     carried over so the history survives the restore),
   - rename `<name>-previous` back to `<name>`, restore its restart policy,
     start it, verify it (same rules).
   - Exit 0 with `{"result":"rolled_back","error":...,"logs":[...]}`; if even the
     restore fails, exit 2 with `{"result":"failed",...}` and leave everything in
     place for an operator (both containers kept, nothing deleted).
9. The helper never deletes itself. The orchestrator reads its logs and exit
   code, stores them, then removes it.

### 3. Manual rollback

`POST .../rollback` launches the same helper with `mode: rollback`: swap the
running target with `<name>-previous` (the previous image AND its original
config), and for the server restore the snapshot taken before the update that
produced the current version (opt out with `{"restoreDb": false}`; when that
snapshot no longer exists the current DB is kept and a warning recorded). A
fresh snapshot of the current DB is taken first and restored if the previous
version does not come up healthy (auto-revert, same verify rules). On success
the rolled-back-from container becomes the new `<name>-previous`, so a rollback
can itself be rolled back. Status of a manual rollback: `updated` when the swap
succeeded, `rolled_back` when it failed and the current version was restored.

### 4. Reconciliation (after restart)

On server startup, then every 5 s while an update is `running` (30 s otherwise):
- find the helper by `kowan.update.id`; when it has exited, parse its JSON lines,
  write the result into `updates` (status, phases, error, logs, finished_at),
  remove the helper container.
- server updates: the new (or rolled back) server performs this on boot.
- agent updates: performed when the agent reconnects; if the agent does not
  reconnect within 10 min the record becomes `unknown` with the last known phase.
- an `unknown` agent record younger than 24 h is still reconciled if its helper
  shows up later.
- local helpers whose record is missing (the DB was restored to a snapshot that
  predates it) are folded into a record rebuilt from the plan in the helper's
  own command line; exited preflight containers and helpers of already-final
  records are removed.
- a running record whose helper was never launched (the server restarted
  mid-orchestration) becomes `aborted` after 2 min.
- there is only ever one `<name>-previous` (older ones are retired at park and
  removed at commit).

### 5. Preflight command

`python -m kowan.server preflight` / `python -m kowan.agent preflight`: import all
modules, parse config from env, check the self-update helper of the image, for
the server copy the database given by `--db` into a temp dir and run the
migrations (and, when the secret key is available, the credential migration)
against the copy. Exit 0/1, JSON summary on the last stdout line
(`{ok, component, checks: [{name, ok, detail|error}]}`). Never touches the live
database.

`python -m kowan.server healthcheck` (GET `/health` on `KOWAN_ADDR`, https when
`KOWAN_TLS_CERT` is set) and `python -m kowan.agent healthcheck` (heartbeat file
`KOWAN_AGENT_HEARTBEAT_FILE`, default `/tmp/kowan-agent.heartbeat`, touched every
10 s while the edge session is up or the direct listener serves; stale after
45 s) are the image HEALTHCHECK commands.

## API (all under /api/v1)

Server (admin):
- `GET  /system/server/update` → status:
  `{supported, reason, error, image, container, version, running, latest,
    updateAvailable, canRollback, previous: {image, version, digest} | null,
    inProgress: Update | null, last: Update | null}`
  (`reason` explains `supported: false`; `error` also carries a best-effort
  registry check failure while `supported` stays true. `running`/`digest` are
  the registry digest, or the image id for a locally built image.)
  The status also carries the release versions, known even when `supported`
  is false (a server run from source): `currentVersion`, `latestVersion` (the
  highest `X.Y.Z` tag of `KOWAN_RELEASE_REPOSITORY`, `ghcr.io/kowan-lab/kowan`
  by default), `newVersion` and `releaseError`; `updateAvailable` is also true
  when `newVersion` is. Registry answers are cached (5 minutes for digests, an
  hour for versions); `?refresh=1`, sent by the Check button, skips both.

Release notices: every 6 hours (first check 2 minutes after start) the server
reads the release versions and, the first time one is newer than its own,
posts a notice to the bell ("Kowan X.Y.Z is available"), once per version
(`kowan/releases.py`). An empty `KOWAN_RELEASE_REPOSITORY`, or `off`, turns the
check off.
- `POST /system/server/update` body `{image?: string}` → `202 {id}`
- `POST /system/server/rollback` body `{restoreDb?: boolean}` (default true) → `202 {id}`
- `GET  /system/server/updates?limit=20` → `Update[]` (history, newest first)
- `GET  /system/updates/{id}` → `Update` (any component; used for polling;
  operator+, server records admin only)

Errors: `400 {error}` when the component cannot be updated (not supported,
invalid body), `409 {error}` when an update of the same component/host is
already running or there is nothing to roll back to, `404` unknown host/update.

Agents (check: operator, act: admin):
- `GET  /hosts/{hostID}/agent/update` → same status shape for the agent
- `POST /hosts/{hostID}/agent/update` body `{image?}` → `202 {id}`
- `POST /hosts/{hostID}/agent/rollback` → `202 {id}`
- `GET  /hosts/{hostID}/agent/updates?limit=20` → `Update[]`
- `POST /agents/update` body `{hostIds: string[], image?}` → `202 {batchId, ids}`:
  every host is updated at the same time, each with its own health check and
  rollback, so a failure on one does not stop the others. All records are
  created up front (status `running`, first phase `queued`).

`Update` object:
```
{ id, component: "server" | "agent", hostId: string | null,
  mode: "update" | "rollback",
  status: "running" | "updated" | "rolled_back" | "failed" | "aborted" | "unknown",
  fromImage, fromDigest, fromVersion, toImage, toDigest, toVersion,
  phases: [{ phase, ok, at, detail }], error, logs: string[],
  startedAt, finishedAt, startedBy, batchId }
```

Phases recorded by the orchestrator: `lock`/`queued`, `resolve`, `pull`,
`platform`, `compare` (only when it aborts), `snapshot` (server), `preflight`
(update), `launch`; then the helper's: `wait`, `inspect`, `stop`, `park`,
`create`, `start`, `verify`, `commit` | `rollback`, `restore-db`, `restore`,
`cleanup`. `logs` holds, on failure, the manual recovery steps (status
`failed`), the last 200 lines of the failed container and helper warnings.

Version: images carry `org.opencontainers.image.version` (build arg `VERSION`,
default `dev`); the API reports it next to the digest.

## Storage

Table `updates` (id TEXT PK, component, host_id, mode, status, from_image,
from_digest, from_version, to_image, to_digest, to_version, phases JSON, error,
logs JSON, started_at, finished_at, started_by, batch_id) with index on
(component, host_id, started_at). Retention: last 50 per component/host.

## Failure matrix (what the user ends up with)

| Failure point | Result |
|---|---|
| pull (image not on the host) / arch mismatch / same image / preflight | nothing touched, `aborted` |
| helper cannot start | nothing touched, `failed` (reported by orchestrator) |
| stop / park | target restarted as is, `rolled_back` |
| create / start / verify | new removed, previous restored (+DB), `rolled_back` |
| rollback itself fails | both containers kept, `failed`, operator steps in logs |
| server dies mid-helper | helper is independent; reconciled on next boot |
| host reboot mid-helper | after `create`: the new container (cloned restart policy) comes back and is reconciled; between `park` and `create`: target left stopped as `<name>-previous` (restart `no`), manual recovery, see limitations |

## Implementation notes (as built)

Code: `kowan/updater/` (`plan`, `clone`, `verify`, `engine`, `dbsnap`, `helper`,
`orchestrator`, `reconcile`, `preflight`), store mixin `kowan/store/updates.py`,
handlers `kowan/api/serverupdate.py` + the agent section of `kowan/api/hosts.py`.
`kowan/serverupdate.py` holds the shared `UpdateManager` accessor and
`kowan/agentupdate.py` the image reference / digest helpers.

- **Clone fidelity.** The new container is created from the target's raw
  inspect, minus every value inherited from the OLD image (Cmd, Entrypoint,
  Healthcheck, image Env/labels, ...) so the new image's defaults apply, with
  anonymous volumes re-bound by name (otherwise `VOLUME /data` without `-v`
  would silently lose the database) and network aliases preserved.
- **Pull fallback.** If the registry pull fails but the image is present on the
  host (locally built, air-gapped), the update continues and the `pull` phase
  says so; if it is absent the update aborts.
- **Same image.** Updating to the image the target already runs is `aborted`
  ("already up to date") instead of a pointless restart.
- **Server data on a mount.** If `KOWAN_DATA_DIR` is not on a volume/bind mount
  of the server container, the server reports `supported: false` (an update
  would lose the database).
- **Deadlines everywhere.** Each Engine call in the helper has a 60 s deadline
  (stop: timeout + 30 s), verify has its own deadline, preflight 60 s, the lock
  goes stale after 30 min, a rolling-batch step waits at most 20 min.

Known limitations:

- A host reboot (or daemon restart) in the seconds between `park` and the new
  container's creation leaves the target stopped as `<name>-previous` with
  restart `no`, and nothing on that host can reconcile it (the server IS the
  target; an agent host cannot be reached without its agent). Recovery is
  `docker rename <name>-previous <name> && docker update --restart unless-stopped <name> && docker start <name>`.
- Helper and preflight containers run from images that define a HEALTHCHECK and
  inherit it (`ContainerSpec` has no healthcheck override), so Docker briefly
  shows them `unhealthy`. Harmless: nothing acts on it.
- Old images are never removed (the parked container keeps the previous one).
- A manual rollback with DB restore discards data written since the update it
  rolls back (that is its purpose; the discarded DB is kept in
  `backups/rolled-back-*`).
