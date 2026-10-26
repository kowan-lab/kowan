# Retained, searchable logs

Workloads opt in to keeping their logs for a number of days. The server then
follows the logs of their running containers on every host and stores the
lines in a full-text index, searchable across hosts, restarts and redeploys.

Code: `kowan/logstore/` (`store.py`: SQLite + FTS5, `collector.py`),
`kowan/api/logarchive.py`, `kowan/workloads.py` (workload ids); UI: Logs →
History (`components/logs/LogHistory.tsx`) and "Keep logs" on a workload's
Logs tab (`components/logs/LogRetentionControl.tsx`).

## Storage

A separate database, `logs.db` in the data dir, outside the configuration
backup. Lines carry the Docker timestamp normalised to a fixed width (string
order is time order) and the source container (host, id, name, workload,
compose project, deployment, labels for access checks). Pruning runs hourly:
lines older than their workload's retention go, lines of workloads that no
longer keep logs go, and above 2 million lines in total the oldest go.

Workload ids are the UI's: `dep~<deployment>`, `stack~<host>~<project>`,
`ctr~<host>~<name>`. A compose project owned by a deployment counts as that
deployment.

## Collector

Every 5 s it lists the running containers of every host and keeps one
follower per container of a workload with a retention. A follower re-reads a
short tail after a restart and skips what is already stored; lines are
written in batches.

## API

```
GET    /api/v1/log-archive/retention
PUT    /api/v1/log-archive/retention/{workload} {days: 1..365}    (operator)
DELETE /api/v1/log-archive/retention/{workload}                   (operator; deletes the stored lines)
GET    /api/v1/log-archive/search?q=&workload=&host=&hours=&since=&until=&before=&limit=
```

Search: words must all appear, `"a phrase"` as is, `-word` excludes; FTS
operators typed by a user are plain text. Newest first; `next` is the
`before` for the following page. Team-scoped users only find lines of
containers they may read.
