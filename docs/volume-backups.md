# Volume backups (restic)

Encrypted, deduplicated backups of Docker volumes to S3-compatible storage,
SFTP, a restic REST server or a path on a host, with schedules, retention,
hooks, restore and snapshot browsing.

Code: `kowan/volbackup/` (`restic.py` pure helpers, `executor.py`,
`service.py`), `kowan/api/volbackups.py`, `kowan/store/volbackups.py`; UI:
`pages/Backups.tsx`, `components/volbackup/` (`VolumeBackupSection` on volume
detail, `DeploymentBackupSection` on deployment detail).

## Repositories

| type | config | secrets |
|---|---|---|
| `local` | `path` (absolute, on `hostId`'s disk) | `password` |
| `s3` | `endpoint` (empty = AWS; `http://` for plain MinIO), `bucket`, `prefix`, `region` | `password`, `accessKeyId`, `secretAccessKey` |
| `sftp` | `host`, `port`, `user`, `path` | `password`, `sshKey` |
| `rest` | `url`, `username` | `password`, `restPassword` |

All types accept `network`: a Docker network the restic container joins (for
example to reach a MinIO container by name). `password` is the restic
repository password: without it snapshots cannot be decrypted.

Secrets are sealed with Kowan's AES-256-GCM key (the same cipher as the
credentials table), returned only as `********`, and a blank or masked value
on update keeps the stored one. Repositories are admin-only to create, edit
and delete. A local repository can only be used from its own host. SFTP
accepts the server's host key on first use (`StrictHostKeyChecking=accept-new`).
The repository is initialised on first use (or with "Test").

## Jobs

- **Volumes:** `selector.mode` = `names` (`volumes: [...]`), `deployment`
  (`deploymentId`: named volumes mounted by its containers, plus volumes of its
  compose project), `stack` (`stack`: compose project containers' volumes and
  volumes labelled with the project), or `label` (volumes or containers
  labelled `kowan.backup=true`). Optional `exclude`. Resolved at each run.
- **Schedule:** cron + timezone (empty cron = manual only). Same runner and
  missed-run policy as [schedules](schedules.md).
- **Retention:** `keepLast/Hourly/Daily/Weekly/Monthly/Yearly`; after each
  backup `restic forget --prune --tag kowan-job=<id> --group-by host,paths`
  (so retention applies per volume and only to this job's snapshots). All
  zero = keep everything.
- **Hooks:** `stopContainers` stops the running containers that mount the
  volumes and starts them again afterwards, even when the backup fails; `pre`
  and `post` run `sh -c <command>` in a running container (e.g. `pg_dump` into
  the volume). A failing pre hook aborts the backup; a failing post hook turns
  the run into a warning.
- **Tags** are added to every snapshot.

## Execution

One short-lived `restic/restic:0.17.3` container per operation on the job's
host, created through the Docker `Ops` interface (so remote agents work with no
new agent RPC). Volumes are mounted read-only at `/data/<name>`; the
repository env is passed as container env. The script:

1. `restic cat config`; exit code 10 (no repository) → `restic init`;
2. per volume `restic backup --json --host <hostId> --tag kowan --tag
   kowan-volume=<name> --tag kowan-job=<id> [tags] /data/<name>` (one snapshot
   per volume);
3. `restic forget --json --prune ...` when retention is set.

Marker lines (`@@kowan phase`, `@@kowan rc`) split the log per phase; the JSON
`summary` gives files new/changed/total, bytes added and duration. restic exit
code 3 (some files unreadable, snapshot still written) is a **warning**.
Mutating operations on one repository are serialised; listing and browsing use
`--no-lock`. Run logs drop restic's progress lines, have every secret value
redacted and are capped at 256 KiB. Runs are kept 180 days; runs left
`running` by a server restart are marked failed at startup.

**Failure alert:** a failed job run creates a `critical` alert (`resource
volbackup:<job>`) and publishes it on the bus, so it reaches the notification
channels and the bell. The next successful run resolves it.

## Restore and browse

- Restore a snapshot into **its volume** (stops and restarts the containers
  using it; `exact` adds `restic restore --delete`, removing files added since)
  or into a **new volume** (refused if the name exists).
- Browse lists one directory of a snapshot at a time (`restic ls --json`), up
  to 5000 entries.
- Volume detail lists the volume's snapshots across the repositories used by
  that host's jobs or ad-hoc runs (cached 30 s), and offers "Back up now"
  (ad-hoc, no retention) and "New scheduled job".

## API (`/api/v1/volbackups`)

| method | path | role |
|---|---|---|
| GET | `/repos` | viewer |
| POST, PUT `/{id}`, DELETE `/{id}` | `/repos` | admin (delete refused while jobs use it) |
| POST | `/repos/{id}/test` | operator |
| GET | `/repos/{id}/snapshots?hostId&volume&fresh` | operator |
| GET | `/repos/{id}/snapshots/{snap}/ls?path&hostId` | operator |
| GET | `/jobs?hostId&deploymentId` (adds `nextRun`, `lastRun`, `running`) | viewer |
| POST, PUT `/{id}`, DELETE `/{id}` | `/jobs` | operator |
| POST | `/jobs/preview` `{hostId, selector}` → `{volumes}` | operator |
| POST | `/jobs/{id}/run` → 202 run | operator |
| GET | `/runs?jobId&volume&hostId&limit`, `/runs/{id}` (with log) | viewer |
| GET | `/volumes/{hostID}/{name}/snapshots` | operator |
| POST | `/volumes/{hostID}/{name}/backup` `{repoId}` → 202 | operator |
| POST | `/restore` `{hostId, repoId, snapshotId, target, newVolume, stopContainers, exact}` → 202 | operator |

## Limitations

- Repository credentials are passed as environment variables of the helper
  container, so they are visible to `docker inspect` on that host while it
  runs (seconds to minutes; the container is removed afterwards).
- Only named volumes are backed up (no bind mounts).
- `exec` hooks report the command's exit status through an `@@exit=` marker,
  since the Ops exec call returns output only.
- Snapshot size shown is restic's `total_bytes_processed` (logical size).
