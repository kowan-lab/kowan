---
title: Runbooks
description: Named actions on your workloads, such as emptying a cache, migrating a database or reloading a configuration. Run them with a click, with parameters, live output and a history, from the console, a schedule or an AI assistant.
---

A runbook is an action you run often on a workload: empty a cache, migrate a database, reload a configuration, rotate a log. You define it once; then anyone with access runs it with a click, without opening a console, and every run is kept with its output.

You find runbooks in the **Runbooks** tab of a workload, and all of them under **Automation → Runbooks**. Press <kbd>⌘K</kbd> and type a runbook's name to run it from anywhere.

## Steps

A runbook is one or more steps, run in order. Each step is a shell command (`sh -c`) on one service of a workload, and runs:

- **In a running container**, like `docker exec`: on one replica, or on every replica in turn.
- **In a throwaway container**, like `docker compose run`: a copy of the service with the same image, environment, volumes and networks, but no published ports. It is removed when the step ends. Use it for migrations and one-off scripts.

A step that fails stops the run, unless it is marked **May fail**: then the failure is reported and the next steps still run. Steps can act on different workloads, so one runbook can, for example, put an app in maintenance, migrate its database and restart its workers.

Each step has a timeout, 10 minutes by default and up to an hour.

## Parameters

A runbook can ask for values when it runs: text, a number, or a choice from a list, with a default and optionally required. The command reads each one as an environment variable:

```sh
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "VACUUM ANALYZE $TABLE;"
```

Values are passed as data, never pasted into the command, so a value such as `x; rm -rf /` stays text.

## Confirmation

Mark a runbook **Ask for confirmation** when it deletes or changes data. The run dialog then asks to confirm, and an AI assistant has to ask you first.

## Runs and history

The run dialog shows each step as it happens: the container it runs in, its output, its exit code. The **History** of a runbook lists every run, who started it (or a schedule, or an assistant), the parameters and the outcome; open any run to read its output again.

## Suggestions

On a workload's Runbooks tab, Kowan suggests runbooks from the images of its services: Redis memory and flush, PostgreSQL vacuum and sizes, MySQL and MariaDB checks, MongoDB stats, nginx reload, RabbitMQ queues, plus disk usage and processes for any container. **Add** opens it in the editor to adjust before saving.

## Declaring runbooks with labels

A runbook can live in the Compose file, next to the service it acts on:

```yaml
services:
  api:
    image: ghcr.io/acme/api:2.1
    labels:
      kowan.runbook.migrate.command: python manage.py migrate
      kowan.runbook.migrate.title: Migrate the database
      kowan.runbook.migrate.mode: run
      kowan.runbook.migrate.confirm: "true"
      kowan.runbook.migrate.params: "APP, VERBOSITY=1"
```

| Label | Meaning |
| --- | --- |
| `kowan.runbook.<name>.command` | The command (required). |
| `kowan.runbook.<name>.title` | The name shown in the console (default: `<name>`). |
| `kowan.runbook.<name>.description` | A line about what it does. |
| `kowan.runbook.<name>.mode` | `exec` (default) or `run` (throwaway container). |
| `kowan.runbook.<name>.replicas` | `one` (default) or `all`. |
| `kowan.runbook.<name>.confirm` | `true` to ask for confirmation. |
| `kowan.runbook.<name>.params` | Parameter names, comma-separated, each with an optional `=default`. |
| `kowan.runbook.<name>.timeout` | Seconds (default 600). |

These runbooks show **From labels** and are read-only in the console: change them in the Compose file.

## Running on a schedule

In [Schedules](schedules.md), choose a runbook as the target and the `run` action. It runs with the parameters' defaults, so a required parameter needs a default.

## When an alert fires

An [alert rule](alerts-and-notifications.md) can run a runbook when it fires: choose it in **Run a runbook** in the rule. It runs with its parameters' defaults, at most once every few minutes for the same container (10 by default), and its steps can read what fired:

| Variable | Value |
| --- | --- |
| `KOWAN_ALERT_RULE` | The rule's name |
| `KOWAN_ALERT_LEVEL` | `info`, `warning` or `critical` |
| `KOWAN_ALERT_HOST` | The host id |
| `KOWAN_ALERT_CONTAINER` | The container's name (empty for a host alert) |
| `KOWAN_ALERT_MESSAGE` | The alert text |

How the run went appears in the notifications. A runbook with a required parameter that has no default cannot be linked to a rule.

## On a host group

A step can target **Group · name**: it then runs on the service (or container) of that name on every host in the [host group](host-groups.md), for example to reload nginx on every edge server.

## AI assistants

Through the [MCP server](mcp.md), `list_runbooks` shows the runbooks of the workloads the assistant can see, and `run_runbook` runs one and returns each step's output. Assistants can then do the actions you approved, instead of arbitrary commands.

## Who can do what

| Action | Needs |
| --- | --- |
| See runbooks and their runs | Read access to every workload the runbook touches |
| Run a runbook | Operate on those workloads |
| Create, edit or delete one | Manage on those workloads |

A team member's permissions are checked when the run starts; the steps then run as approved, including the throwaway containers, which a team member could not create by hand.

## Good to know

- The command runs with `sh`, which the image must have.
- A step in a running container has no input: a command waiting for one waits until its timeout.
- A runbook does not run twice at the same time.
- Each runbook keeps its last 100 runs, with the end of each step's output (64 KB).
