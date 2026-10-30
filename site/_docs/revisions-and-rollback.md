---
title: Revisions and rollback
description: Every change to a Kowan deployment is saved as a numbered revision. Compare any two as a YAML diff and roll back to an earlier one in one click.
---

Every time you save a change to what a [deployment](deployments.md) runs, Kowan keeps the previous configuration as a numbered revision. You can see who changed what and why, compare any two revisions, and restore an older one when a change goes wrong.

## What a revision holds

A revision is a snapshot of everything that decides what runs:

- the name, type, strategy, host and replicas;
- the container settings (fields left at their defaults are left out) or the Compose file;
- the stack variables of a Compose deployment;
- for a [Git deployment](deployments.md#deploy-from-a-git-repository), its repository settings and the commit it ran.

Switches that do not change the containers are not part of it: auto-heal, deletion protection, status and timestamps. Toggling them records nothing. Secret values are not in revisions either: a secret variable keeps only its reference, so a rollback keeps the current secret value.

Kowan records a revision only when the snapshot differs from the latest one, and keeps the last 100 revisions of each deployment.

### Where a revision comes from

Each revision records its author and its source:

| Source | Created by |
| --- | --- |
| `create` | Creating the deployment |
| `edit` | Saving the **Configuration** tab, or an edit through the API |
| `git` | A deploy that ran a new commit of a Git deployment |
| `gitops` | A [GitOps](gitops.md) sync |
| `exposure` | Publishing or unpublishing a domain on [Exposure](exposure-and-https.md) |
| `mcp` | An [AI assistant](mcp.md) |
| `rollback` | A rollback |
| `preview` | A preview environment being created or updated |
| `promote` | A [promotion](previews-and-promotion.md) into this deployment |
| `import` | [Importing](importing.md) the stack |
| `registry` | Deploying a registry from [Registries](registries.md#deploy-your-own-registry) |
| `tags` | Applying newer version tags from [Image updates](image-updates.md) |

## View the history

Open the deployment and select the **History** tab.

The **Revisions** list shows the newest first, with the number, the source, when and by whom, and the fields that changed (such as `compose`, `container`, `host` or `commit`). The revision running now is marked **Live**.

Select a revision to see what it changed from the one before it, as a line diff of the configuration in YAML. To compare it with another revision instead, pick one in **Compare with…**.

Viewers see a reduced history: environment values are hidden, Compose files show only each service's image, ports, dependencies, restart policy, networks and volumes, and credentials are removed from Git URLs.

## Roll back

1. On the **History** tab, select the revision you want to go back to.
2. Click **Roll back to #N**.
3. Leave **Deploy it now** ticked to redeploy at once, or untick it to only save the configuration.
4. Click **Roll back**.

A rollback never rewrites history. The old configuration is saved as a new revision on top, so you can roll forward again the same way. You need the operator role.

!!! note "Git deployments are pinned"
    Rolling back a Git deployment restores the exact commit that revision ran and turns **Auto deploy on push** off, so the next push does not undo the rollback. When you are ready to follow the branch again, set the branch back in **Branch or tag** on the **Configuration** tab and turn auto deploy on.

Kowan refuses a rollback that would:

- give the deployment a name another deployment already uses;
- target a host that no longer exists;
- fail [Compose validation](compose-validation.md) on the host as it is today.

## From the command line

```bash
kowanctl deployment history <id>                # list revisions, newest first
kowanctl deployment diff <id> 7                 # what revision 7 changed
kowanctl deployment diff <id> 7 --against 4     # compare 7 with 4
kowanctl deployment rollback <id> 4             # restore revision 4 and redeploy
kowanctl deployment rollback <id> 4 --no-deploy # restore without deploying
```

In `history`, a `*` marks the revision running now. See [Command line](cli.md).

## API

| Request | Purpose |
| --- | --- |
| `GET /api/v1/deployments/{id}/revisions` | List revisions |
| `GET /api/v1/deployments/{id}/revisions/{n}` | One revision with its configuration |
| `GET /api/v1/deployments/{id}/revisions/{n}/diff?against=m` | Diff with revision `m` (default: the previous one) |
| `POST /api/v1/deployments/{id}/revisions/{n}/rollback` | Roll back. Body `{"deploy": false}` saves without deploying. Operator role. |

See [REST API](api.md).
