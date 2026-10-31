# Runbooks

Named, approved actions on workloads (user guide: `site/_docs/runbooks.md`).

## Model (`kowan/runbooks/model.py`)

A runbook is `{name, description, confirm, params[], steps[]}`, stored in
`runbooks` (`kowan/store/runbooks.py`). A step is `{title, workload, service,
mode: exec|run, replicas: one|all, command, continueOnError, timeoutSeconds}`
on a workload id (`kowan/workloads.py`); steps may target different
workloads. Parameters are `{name (env var), label, type: text|number|choice,
choices, default, required}`.

Runbooks from labels (`kowan.runbook.<key>.*` on a service's containers) are
read live, never stored; their id is `label:<workload>:<service>:<key>` and
they are read-only.

## Running (`kowan/runbooks/service.py`)

`POST /runbooks/{id}/run` checks operate on every workload, then starts a
background task and answers 202 with the run. The task binds no principal:
access was decided at the start, and a `run` step needs to create a container
that a team member's Docker view would refuse.

Each step runs `sh -c` with this script:

```sh
export NAME='value'        # shlex.quote: parameters are data
(
<command>
)
printf '__KOWAN_EXIT_<random>_%s__\n' "$?"
```

The subshell keeps the command's own `exit` from skipping the marker; the
marker carries the exit code and is cut from the output (`_Output`, also when
it arrives split across chunks). This works the same through `exec_attach`
(TTY, raw output) locally and over an agent, with no new agent operation.

- `exec`: in the running containers of the service (the first, or each).
- `run`: `container_spec` of a container of the service, without ports and
  restart, with the workload's compose/stack labels plus `kowan.support` and
  `kowan.runbook-run`; started, followed through its logs (timestamps cut),
  removed in any case.

Runs are saved in `runbook_runs` as they progress (each step record keeps the
last 64 KB of output; 100 runs per runbook). Live viewers follow
`GET /runbook-runs/{id}/ws` (`step`, `output`, `done` events; a late viewer
gets everything so far; a finished run replays from the store). A runbook
does not run twice at once (409). Runs left running by a restart are marked
failed at startup.

## Other entry points

- Schedules: target `runbook`, action `run`, with the parameter defaults.
- MCP: `list_runbooks`, `run_runbook` (`kowan/mcp/tools/runbooks.py`).
- `GET /runbooks/suggestions?workload=`: templates from the services' images.

## Permissions

Read on every workload to see a runbook and its runs, operate to run it,
manage to create, change or delete it (`TargetAccess`, as schedules).
