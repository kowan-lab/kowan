---
title: Compose validation
description: How Kowan checks a Docker Compose file before it deploys: syntax, port and name conflicts on the host, missing secrets and risky settings, with one-click fixes.
---

Kowan checks a Compose file while you edit it and again before it is saved or deployed. It catches what would make `docker compose up` fail, or quietly misbehave, on the target host: a port already in use, a volume nobody declared, a secret that does not exist. Errors block the deploy; warnings and tips do not.

## Where validation runs

In the editor
:   The **Compose** section of the deployment form validates the file shortly after you stop typing, in both the **Easy** and the **YAML** editor. Results appear in the **Validation** panel under the editor, and the YAML editor marks the lines.

When you save or deploy
:   Creating, editing, deploying, [rolling back](revisions-and-rollback.md), [promoting](previews-and-promotion.md) and [importing](importing.md) an inline Compose deployment all go through the same check on the server. Any error refuses the change with the list of problems.

In GitOps
:   A `Deployment` resource with Compose content is checked when a [GitOps](gitops.md) sync computes its plan. An error fails the plan, and a plan with errors is never applied.

A Compose file read from a Git repository by a [Git deployment](deployments.md#deploy-from-a-git-repository) is not checked by this gate.

## Read and fix the results

Each finding has a severity, a message and, when known, a position.

| Severity | Shown as | Effect |
| --- | --- | --- |
| Error | **Error** | Blocks saving and deploying until you fix it. |
| Warning | **Warn** | Allowed, after you confirm. |
| Info | **Info** | A tip. Never blocks. |

To work through them:

1. Click a finding's position to jump to that line in the YAML editor. From the **Easy** editor, this switches to **YAML**.
2. When Kowan knows a safe fix, click **Apply fix**. It edits the file in place and keeps your comments and layout.
3. Fix the remaining errors by hand. **No problems found** means the file is clean.
4. If warnings remain, tick **Deploy with N warnings** to confirm you reviewed them. The confirmation covers exactly that set of warnings: a new one asks again.

## What Kowan checks

### Structure

Kowan runs `docker compose config` on the file, as Docker itself would read it, and maps the error messages back to a line. The Kowan server image includes the Compose plugin; when it is not available, Kowan falls back to a built-in check of the file's shape (top-level keys, service structure, list and map types, undeclared networks, duplicate keys). YAML syntax errors are reported with the parser's position.

Kowan secret references such as `[[db.password]]` never break the YAML check, and an `env_file` is read on the target host, so a missing one is reported as info, not as an error.

### Rules

| Check | Severity | Apply fix |
| --- | --- | --- |
| Image without a tag (so it means `:latest`) | Warning | |
| Image pinned to `:latest` | Warning | |
| Service without a restart policy (`deploy.restart_policy` counts) | Info | Adds `restart: unless-stopped` |
| `privileged: true` | Warning | |
| `network_mode: host` | Warning | |
| `pid: host` | Warning | |
| The Docker socket is mounted | Warning | |
| A published host port is already used by a running container on the host | Error | Publishes on the next free port |
| Two services publish the same host port | Error | |
| An absolute bind-mount source is missing on the host | Warning (short syntax, Docker creates an empty root-owned directory) or error (long syntax, the deploy fails) | |
| A named volume is not declared under top-level `volumes:` | Error | Declares it |
| A service that others depend on has no healthcheck | Info | |
| `condition: service_healthy` on a service without a healthcheck | Warning | |
| `depends_on` names a service that is not in the file | Error | Removes the entry |
| Two services share a `container_name` | Error | Removes it |
| A `container_name` is already used by another container on the host | Error | Removes it |
| `${VAR}` has no default and is not set | Warning | |
| `[[name]]` matches no Kowan secret | Error | |
| `[[name.field]]` names a field the secret does not have | Error | |
| A bare `[[name]]` refers to a secret with several fields and no `value` field | Error | |

A variable counts as set when it is one of the deployment's stack variables. A variable with a default (`${TAG:-1.27}`) never warns. To fix a missing variable, add it under **Stack variables** or give it a default.

### Host checks

The port, container name and bind path checks look at the target host. They exclude the deployment's own containers, so a redeploy never conflicts with itself.

- They run only when the host answers within 5 seconds (`KOWAN_COMPOSE_HOST_TIMEOUT`). An offline host skips them silently.
- Bind paths can be checked only when the deployment targets the server's own host and the Kowan server runs directly on it, not in a container. With the usual [container install](installation.md), this check is skipped.

## Validate from the API

`POST /api/v1/compose/validate` runs the same checks without saving anything. It needs the operator role.

```json
{
  "compose": "services:\n  web:\n    image: nginx\n",
  "hostId": "local",
  "deploymentId": "",
  "env": { "TAG": "1.27" }
}
```

`hostId` turns on the host checks, `deploymentId` excludes an existing deployment's containers, and `env` lists the variables you will set. A bad file still answers `200`: the problems come back as `diagnostics`, each with its `severity`, `message`, `line` and `column` (1-based, `0` when unknown) and, when available, a `fix`. The file can be up to 512 KiB. See [REST API](api.md).

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `KOWAN_COMPOSE_VALIDATE` | `1` | Set to `0` to turn off the server-side check on create, edit, deploy, rollback, promotion and import. The deployment form still shows findings and blocks on errors, and GitOps plans are still checked, so this mainly affects the API and `kowanctl`. |
| `KOWAN_COMPOSE_HOST_TIMEOUT` | `5` | Seconds to wait for the target host before skipping the host checks. |
| `KOWAN_COMPOSE_VALIDATE_TIMEOUT` | `10` | Seconds to wait for `docker compose config`. |

!!! warning
    Turning the gate off lets a file with errors reach the host, where `docker compose` fails or starts something you did not intend. Use it only to get past a check you are sure is wrong.

See [Configuration](configuration.md) for how to set environment variables.
