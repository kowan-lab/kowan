# Crash snapshots

User guide: `site/_docs/crash-snapshots.md`.

`kowan/crashes/watcher.py` (`CrashWatcher`, attached as `svc.crashes`) keeps
one task per host following `ops.events(filters={type: container, event: die,
kill, oom})`; a reconcile every 30 s starts tasks for new hosts, stops the
ones of deleted hosts and restarts a stream that ended (15 s pause).

A `die` with a non-zero `exitCode` and no `kill` of the same container in the
30 s before is a crash. It is inspected at once and saved in `crash_snapshots`
(`kowan/store/crashes.py`): summary columns plus `data` JSON (state, config,
mounts, networks, health, env with secret-looking values replaced, labels
without secret-looking keys, last 200 log lines without timestamps, the
workload's metrics of the last 30 min). One snapshot per container per 5 min;
later crashes increment `repeats`. 20 per container, 30 days.

Skipped: `kowan.snapshot=off`, `kowan.support=true`.

API (`kowan/api/crashes.py`): `GET /crash-snapshots?workload=&hostId=&limit=`,
`GET/DELETE /crash-snapshots/{id}`; read access on the workload
(`TargetAccess`), operate to delete. MCP: `crash_snapshots`.

The monitor's alert payload now carries `containerId` / `containerName` too,
for what reacts to alerts (runbooks: `kowan/runbooks/alerts.py`).
