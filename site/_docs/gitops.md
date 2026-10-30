---
title: GitOps
description: Keep Kowan deployments, alert rules, workflows and other settings as YAML in Git, review a plan of every change, and apply it on approval or automatically.
---

GitOps keeps part of Kowan's configuration in a Git repository. A **sync** fetches the repository, validates every resource in it, shows a plan of what would be created, updated or deleted, field by field, and applies it when you approve, or automatically. Use it to review configuration changes in pull requests and keep their history in Git.

GitOps is for admins. An admin can turn it off in **Settings**, which also stops every sync and its webhook.

## Resource files

A sync reads every `.yml` and `.yaml` file under its path, at any depth (hidden files and folders are skipped). A file can hold several documents separated by `---`. Each Kowan resource looks like this:

```yaml
apiVersion: kowan/v1
kind: Deployment
metadata:
  name: web              # unique per kind: this is the resource's identity
  labels: {team: core}   # optional, informational
spec:
  host: local
  composeFile: stacks/web.yml
```

Documents without `apiVersion` and `kind`, such as a plain Compose file that a resource points to, are ignored. A document that claims to be a Kowan resource but is malformed, has an unknown kind, an invalid spec, or the same kind and name as another one is a plan error. A plan with errors is never applied.

Resources refer to each other by name (a host, a notification channel, a deployment, a Git credential), including resources created by the same plan. A sync reads at most 1,000 YAML files of up to 1 MiB each.

### Kinds

Kowan applies kinds in this order, and deletes them in reverse:

| Kind | What it manages | Notes |
| --- | --- | --- |
| `Registry` | An image [registry](registries.md) | `url`, `type`, `username`, `insecure`, `connection`. A `password` is refused: it stays as stored in Kowan, or comes from the connection. |
| `NotificationChannel` | A [notification channel](alerts-and-notifications.md) | `type`, `enabled`, `minLevel`, `config`. Sensitive values must be secret references such as `url: "[[slack-url]]"`. |
| `Template` | A custom app template | Built-in templates are not managed. |
| `Deployment` | A [deployment](deployments.md) | See below. |
| `AlertRule` | An [alert rule](alerts-and-notifications.md) | The host and channel are referenced by name. |
| `Workflow` | A [workflow](workflows-and-webhooks.md) | Trigger secrets and webhook tokens are never in Git and are kept on update. |
| `Schedule` | A scheduled start, stop or restart | See [Schedules](schedules.md). |
| `AutoUpdatePolicy` | An [image update](image-updates.md) policy | `metadata.name` is the target: a deployment name, or `<host>/<container>` with `target: container`. |

### Deployment

```yaml
apiVersion: kowan/v1
kind: Deployment
metadata:
  name: web
spec:
  host: local                    # host name or ID (required)
  type: compose                  # compose (default) or container
  strategy: standalone           # standalone (default) or swarm
  enabled: true                  # auto-heal
  protected: false               # deletion protection
  composeFile: stacks/web.yml    # relative to this file, or /path from the repository root
  # compose: |                   # or the Compose file inline
  # git: {repoUrl: https://github.com/acme/web.git, ref: main, path: docker-compose.yml, credential: github, autoDeploy: true}
  # container: {image: nginx:1.27, ports: [{hostPort: 8080, containerPort: 80}]}
  policies:
    deploy: on-change            # on-change (default) or never
```

`replicas` applies to Swarm deployments. Use one of `compose`, `composeFile` or `git` for a Compose deployment, and `container` for a container deployment. Compose content is checked by [Compose validation](compose-validation.md), and an error fails the plan.

`policies.deploy: on-change` deploys a deployment the sync creates, and redeploys one that is enabled and deployed when what it runs changes. `never` only saves the configuration.

## Create a sync

1. Open **GitOps** and click **New sync**.
2. Fill in the form and click **Save**.
3. Copy the **Push webhook** URL and secret that appear. The secret is shown only now.
4. Add the webhook to your repository: on GitHub or Gitea, use the secret as the webhook secret; on GitLab, as the secret token.
5. Open the sync and click **Refresh** to fetch the repository and compute the first plan.

| Field | Meaning |
| --- | --- |
| **Name** | The sync's name. |
| **Repository URL** | HTTPS or SSH clone URL of the repository holding the resource files. |
| **Branch** | Default `main`. |
| **Path** | The folder to scan, such as `kowan/`. Empty scans the whole repository. |
| **Credential** | A stored Git connection, for a private repository and for pushing exports. |
| **Check for changes** | **Manual / webhook only**, or every minute, 5 minutes (default), 15 minutes or hour. The API accepts any interval of 30 seconds or more. |
| **Apply automatically** | Apply a clean plan as soon as a new commit is found. Off: changes wait for your approval. |
| **Delete removed resources** | On by default. Delete a resource this sync manages when its file disappears from Git. Off: the resource is released and kept. |
| **Enabled** | A disabled sync ignores its interval and webhook. |

## Review and apply a plan

The sync's page shows its status (**In sync**, **Drift** when changes are pending, or **Error** when the fetch or the plan failed) and the **Plan**. For each resource, Kowan finds the current one by kind and name:

**Create**
:   No resource has that name yet.

**Adopt**
:   A resource with that name exists but no sync manages it. Applying adopts it, and updates it if it differs.

**Update** or **No change**
:   The resource is managed by this sync. Expand an update to compare each field's **Current** value with the one **From git**.

**Delete** or **Release**
:   A managed resource whose file was removed. It is deleted when **Delete removed resources** is on, and released (kept, no longer managed) when it is off.

A resource managed by another sync is a plan error: each resource has one owner. Kowan never deletes a resource it does not manage, and [deletion protection](deployments.md#deployment-actions) on a deployment or workflow blocks its delete, with the reason shown in the plan.

To apply, click **Apply** and confirm. Kowan applies exactly the commit you reviewed: if a newer commit arrived in the meantime, the apply fails and asks you to refresh and review again. Deleting a deployment tears down its containers on the host.

Every refresh and apply is recorded under **Runs**, with its plan and the result for each resource. **Managed resources** lists what the sync created or adopted.

### Drift

A change made in the UI to a managed resource shows up as drift at the next refresh, and applying restores the version in Git. To keep a change, commit it to the repository.

## Triggers

A sync refreshes when:

- you click **Refresh**;
- its interval is due (with **Apply automatically**, a clean plan is then applied at once);
- its webhook receives a push to the sync's branch that touches its path. The webhook URL has the form `/hooks/gitops/<token>` and accepts GitHub and Gitea signatures and GitLab tokens. **Rotate secret** in the **Webhook** card issues new values.

Kowan makes a shallow clone with the sync's credential, passed to Git through the environment, never in the URL.

## Export your current setup

To start from what you already have, click **Export** on the **GitOps** page and pick the resources. Kowan writes them in the same format, one file per resource (such as `deployments/web.yaml`):

- **Download**: **Download zip** saves the files, **Preview files** shows them.
- **Commit to a branch**: pick a sync, a **Branch** (created from the sync's branch when missing) and a **Commit message**. Kowan commits under the sync's path and pushes with the sync's credential.

Pointing a sync at an unchanged export gives a plan where every resource is **No change** or **Adopt**.

!!! note "No secrets in Git"
    Exports never contain secrets. Notification channels keep their `[[secret]]` references, and sensitive values without one are left out with a warning. Registry passwords and workflow trigger secrets stay in Kowan.

## Limits

- Renaming a resource in Git deletes the old one and creates a new one.
- A channel's secret value is refreshed only when the channel itself is applied: changing the secret alone is not drift.
- Exporting into a sync's own path next to existing files can define a resource twice, which is a plan error until you remove the old file.
- Deleting a sync keeps the resources it managed and releases them.

The GitOps API lives under `/api/v1/gitops` and requires an admin. See [REST API](api.md).
