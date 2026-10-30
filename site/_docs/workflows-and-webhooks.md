---
title: Workflows and webhooks
description: Automate redeploys and maintenance with workflows: ordered steps run by hand, on cron, from a webhook URL, or when an image, a Git branch or a feed changes.
---

A workflow is a named list of steps that Kowan runs in order: redeploy a deployment, call a URL, run a script or a one-off container, prune a host, send a notification. You start it by hand, on a cron schedule, from a public webhook URL, or when an image, a Git branch or a feed changes.

## Create a workflow

1. Go to **Automation → Workflows** and click **New workflow**.
2. Enter a **Name** and, if you want, a **Description**.
3. Leave **Enabled** on (**Allow scheduled, webhook and polling runs**).
4. Add **Variables** if your steps need them (see [Variables and secrets](#variables-and-secrets)).
5. Under **Triggers**, click **Add trigger** for each thing that should start the workflow. Without triggers, you run it by hand.
6. Under **Steps**, click **Add step** for each step and drag them into order.
7. Click **Create**.

Click **Run now** to try it, then follow the run in the **Runs** tab.

## Steps

| Step | What it does |
| --- | --- |
| **Run script** | Runs a Bash script on the Kowan server. Admins only. Stops after 5 minutes. |
| **HTTP request** | Calls a **URL** with the **Method**, **Headers** and **Body** you set. A **Credential** can add the `Authorization` header. The step fails on a status outside 2xx, or, when you set **Expected status** (`204` or `2xx`), on any other status. Stops after 30 seconds. |
| **Deployment** | Redeploys, deploys, starts, stops or destroys a [deployment](deployments.md). |
| **Container action** | Starts, stops or restarts a container on a host. |
| **Run container** | Runs a one-off container from an **Image** on a host, with an optional **Command** and **Environment**, and captures its output. Fails when the container exits with a non-zero code. Stops after 5 minutes. **Auto-remove** deletes the container afterwards. |
| **Prune** | Removes stopped containers, dangling images, all unused images, unused volumes, unused networks, or everything, on a host. |
| **Send notification** | Sends a **Title** and **Body** at a **Level** through one of your [notification channels](alerts-and-notifications.md). |
| **Backup** | Writes a backup archive of Kowan, keys included, to `<data>/backups`, or to the **Path** you set, a folder inside it. Admins only. See [Backup and restore](backup-and-restore.md). |

When a step fails, the remaining steps are skipped and the run fails, unless the step has **Continue on error**. A whole run stops after one hour.

## Variables and secrets

Variables carry values from one step to the next:

- **Variables** set on the workflow are available from the first step.
- A **Run script** step sees them as environment variables. An **HTTP request** step replaces `$NAME` or `${NAME}` in its URL, headers and body. A reference to a name that does not exist is left as written.
- A script exports a variable to the following steps by writing `KEY=value` lines to the file named in `$KOWAN_OUTPUT`.
- A **Run container** step with an **Output variable** such as `RESULT` exports the container's output as `$RESULT` and its exit code as `$RESULT_EXIT`.

Secrets stored in **Settings → Secrets** are referenced as `[[secret-name]]` or `[[secret-name.field]]` in scripts, HTTP requests and variable values, and resolved when the workflow runs.

A script runs with a minimal environment: your variables plus a few basics such as `PATH` and `HOME`, never the Kowan server's own configuration.

## Triggers

| Trigger | Starts a run when |
| --- | --- |
| **Cron schedule** | Its cron expression matches, in the Kowan server's local time. Five fields, or macros such as `@daily`. |
| **Webhook** | An HTTP request reaches its URL. See [Webhooks](#webhooks). |
| **Docker image** | A tag gets a new image in its registry (a push to the same tag). Pick a **Registry** credential for a private registry; without one, Kowan asks Docker Hub anonymously. |
| **Git commit** | The last commit of a branch changes. Pick a **Credential** for a private repository. |
| **RSS feed** | A new entry appears in an RSS or Atom feed. |

A workflow can have several triggers; any of them starts a run. Each trigger has its own on/off switch. While the workflow is disabled, no trigger fires and its webhook URLs answer `409`; you can still run it by hand.

Kowan checks the Docker image, Git commit and RSS triggers every minute. A new trigger first records what is there and does not fire for it: only later changes start runs. A trigger that cannot reach its source shows an **error** badge with the reason.

!!! tip
    To update containers when their image changes, an [auto-update policy](image-updates.md) is usually simpler than a Docker image trigger: it verifies the new version and rolls back on failure.

## Webhooks

A webhook URL lets another system start a workflow: your CI, a Git host, a monitoring tool. Kowan offers two kinds, both at `/hooks/workflow/<token>` on your Kowan server:

Run webhook
:   On the workflow's **Automation** tab, click **Generate webhook**. It accepts `POST`. **Rotate** replaces the URL (the old one stops working) and **Remove** turns it off.

Webhook trigger
:   Add a **Webhook** trigger under **Triggers** and choose its **Method**: `POST` (the default), `GET`, `PUT`, `PATCH`, `DELETE` or `any`. The **Run URL** appears once you save. Turning the trigger off disables its URL.

```bash
curl -X POST "https://kowan.example.com/hooks/workflow/<token>?env=staging" \
  -H "Content-Type: application/json" \
  -d '{"ref": "main"}'
```

Kowan answers `202` with the ID of the new run, `404` when the token is unknown, and `405` when the method does not match the trigger.

### Use the request in your steps

A **Run script** step receives the request body on standard input, and the request as environment variables:

| Variable | Contains |
| --- | --- |
| `KOWAN_WEBHOOK_METHOD` | The HTTP method |
| `KOWAN_WEBHOOK_BODY` | The request body |
| `KOWAN_WEBHOOK_HEADER_<NAME>` | Each header, its name in capitals with other characters turned into `_`: `X-GitHub-Event` becomes `KOWAN_WEBHOOK_HEADER_X_GITHUB_EVENT` |
| `KOWAN_WEBHOOK_QUERY_<NAME>` | Each query parameter, named the same way |

```bash
#!/bin/bash
payload=$(cat)    # the request body
echo "method: $KOWAN_WEBHOOK_METHOD, env: $KOWAN_WEBHOOK_QUERY_ENV"
echo "TARGET_ENV=$KOWAN_WEBHOOK_QUERY_ENV" >> "$KOWAN_OUTPUT"
```

An **HTTP request** step uses `{{body}}`, `{{method}}`, `{{header.NAME}}` and `{{query.NAME}}` in its URL, headers and body. Values from the request are inserted as plain text, so a caller cannot slip in a secret reference or a variable. Kowan keeps up to 1 MiB of the request body.

!!! warning "Treat webhook URLs as secrets"
    The token in the URL is the only check: Kowan does not verify signatures on workflow webhooks, and [network access rules](network-access.md) do not cover `/hooks/` paths. Anyone who has the URL can start a run, so rotate it if it leaks.

To redeploy a deployment from CI, you don't need a workflow: deployments have their own **Redeploy webhook**, which also checks the push signature of Git hosts for deployments from a Git repository. See [Deployments](deployments.md) and [GitOps](gitops.md).

## Runs

The **Runs** tab lists each run with its status, trigger and start time. Select a run to see the output of every step. Kowan keeps the last 100 runs of each workflow; change this with `KOWAN_WORKFLOW_RUNS_KEEP` (see [Configuration](configuration.md)).

## Permissions

- Viewers see workflows, but not webhook tokens, variable values or run output.
- Operators create, edit and run workflows, and read run history, which can contain script output and HTTP responses.
- A workflow with a **Run script** or a **Backup** step can only be created, changed, run or given a webhook by an admin: a script runs on the Kowan server, and a backup holds the keys to every stored secret.
- `KOWAN_SERVER_SCRIPTS=off` turns script steps off entirely.

See [Users and roles](users-and-roles.md).

## Command line

```bash
kowanctl workflow ls                  # list workflows
kowanctl workflow run <workflow-id>   # start a run
kowanctl workflow runs <workflow-id>  # show the run history
```

See [Command line](cli.md) to install and sign in.
