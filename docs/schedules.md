# Scheduled start/stop

Start, stop or restart containers, deployments and compose stacks on a cron
schedule ("stop the dev stack at 22:00 on weekdays, start it at 08:00").

Code: `kowan/schedules/` (cron, runner, service), `kowan/api/schedules.py`,
`kowan/store/schedules.py`; UI: `pages/Schedules.tsx`,
`components/schedules/` (editor, `ScheduleSection` on container and
deployment detail).

## Model

| target type  | target id                                  | actions                          |
|--------------|--------------------------------------------|----------------------------------|
| `container`  | container **name** (survives a recreate)   | `start`, `stop`, `restart`       |
| `deployment` | Kowan deployment id                         | `start`, `stop`, `restart`, `deploy` |
| `stack`      | compose project (`com.docker.compose.project`) | `start`, `stop`, `restart`   |

`deploy` = redeploy through the deployment service; `restart` of a deployment
is stop then start. Container and stack schedules need a `hostId`.

Fields: `name`, `cron` (5 fields or `@daily`/`@hourly`/…), `timezone` (IANA
name; empty = server local time), `enabled`, `onlyIf.noExecSession`
(container stop/restart only: skip while Docker lists exec instances on the
container; best effort, an exec that just ended can still cause one skip).

## Labels

```
kowan.schedule.stop=0 22 * * 1-5
kowan.schedule.start=0 8 * * 1-5
kowan.schedule.restart=0 4 * * 0
kowan.schedule.timezone=Europe/Rome     # optional
```

Labels are discovered by scanning every host's containers every 5 minutes
(and on `POST /schedules/rescan`). They appear as read-only schedules with
`source: "label"` and id `label:<host>:<container>:<action>`. An invalid cron
or timezone shows the schedule with an `error` and never runs it.

**Precedence:** a stored schedule for the same host + container + action
overrides the label, whether enabled or not (disabling the stored schedule is
how an operator silences a label without touching the container).

## Scheduler

`kowan/schedules/runner.py::CronRunner` (shared with volume backups) keeps a
computed `next_due` instant per entry and sleeps until the earliest one (max
30 s). Missed-run policy:

- a slot more than **10 minutes** late (server down, loop stalled) is not run
  late: a `skipped` run with "missed: N min late" is recorded, and the entry
  moves to its next future slot. Only the oldest missed slot is reported;
- a slot whose previous run is still going is skipped ("previous run still in
  progress");
- after a restart the next slot is computed from the last recorded run, so a
  slot missed during downtime is detected.

DST: a wall-clock time that does not exist (spring forward) runs once at the
first valid minute after the gap; a time that happens twice (fall back) runs
once, at the first occurrence. The workflow scheduler is unchanged; its
5-field parser is reused.

Run history (`schedule_runs`) keeps 90 days. A failed run posts a warning
notice to the notification bell.

## API (`/api/v1`)

| method | path | role |
|---|---|---|
| GET | `/schedules?targetType&targetId&hostId` | viewer |
| POST | `/schedules` | operator |
| GET/PUT/DELETE | `/schedules/{id}` | viewer / operator / operator |
| POST | `/schedules/{id}/run` | operator (also label schedules) |
| GET | `/schedules/runs?scheduleId&limit` | viewer |
| POST | `/schedules/preview` `{cron, timezone, count}` → `{nextRuns}` | viewer |
| POST | `/schedules/rescan` | operator |

Each schedule in a list carries `nextRun`, `lastRunAt` and `lastStatus`.
