# GitOps: configuration as code

A **sync** keeps part of Kowan's configuration in a git repository. It
fetches the repository, validates every resource, shows a plan (what would be
created, updated or deleted, field by field), and applies it when you approve,
or automatically.

## File format

Any `*.yml` / `*.yaml` under the sync's path (any depth, hidden dirs skipped,
`---` separated documents allowed):

```yaml
apiVersion: kowan/v1
kind: Deployment            # see "Kinds"
metadata:
  name: web                 # unique per kind; this is the identity
  labels: {team: core}      # optional, informational
spec: { ... }
```

Documents without `apiVersion`/`kind` (a plain compose file referenced by
`composeFile`, say) are ignored. A document that claims to be a Kowan resource
but is malformed, an unknown kind, an invalid spec or a duplicate
`(kind, name)` is a plan **error**, and a plan with errors is never applied.

Resources reference each other **by name** (host, channel, deployment, git
credential), including resources created by the same plan.

### Kinds

Applied in this order (deletes in reverse):

| kind | order | notes |
|---|---|---|
| `Registry` | 10 | `url`, `type`, `username`, `insecure`, `connection` (shared connection name). No password: it stays as stored in Kowan. |
| `NotificationChannel` | 20 | `type`, `enabled`, `minLevel`, `config`. Sensitive config values must be secret refs (`url: "[[slack-url]]"`), resolved when applied. |
| `Template` | 30 | custom app templates (`type`, `compose` or `spec`, `inputs`, ...). |
| `Deployment` | 40 | see below |
| `AlertRule` | 50 | `type`, `operator`, `threshold`, `severity`, `forDuration`, `host`, `match`, `labelKey`, `labelValue`, `channel`, `autoResolve`, `enabled` |
| `Workflow` | 60 | `description`, `enabled`, `variables`, `steps`, `triggers`. Step `channel` / `host` / `deployment` by name. Trigger secrets and webhook tokens are never in git and are kept on update. |
| `Schedule` | 70 | scheduled start/stop: `targetType`, `target` (deployment name, container or stack), `host`, `action`, `cron`, `timezone`, `enabled`, `onlyIf` |
| `AutoUpdatePolicy` | 70 | `metadata.name` is the target: a deployment name, or `<host>/<container>` with `target: container`; `mode`, `window`, `minAge`, `scanBlock` |

`Schedule` and `AutoUpdatePolicy` are active when the server has those
subsystems. `GET /api/v1/gitops/kinds` lists what the server supports.

### Deployment

```yaml
apiVersion: kowan/v1
kind: Deployment
metadata: {name: web}
spec:
  host: local                    # host name or id (required)
  type: compose                  # compose (default) | container
  strategy: standalone           # standalone (default) | swarm
  replicas: 2                    # swarm only
  enabled: true
  protected: false
  composeFile: stacks/web.yml    # relative to this file (or /repo/root path)
  # compose: |                   # ...or inline
  # git: {repoUrl: ..., ref: main, path: docker-compose.yml, credential: gh, autoDeploy: true}
  # container: {image: nginx:1.27, ports: [{hostPort: 8080, containerPort: 80}]}
  policies:
    deploy: on-change            # on-change (default) | never
```

Compose is validated with the same checks as the deployment form (errors fail
the plan). `policies.deploy: on-change` deploys a newly created deployment and
redeploys an enabled, deployed one whose runtime fields changed; `never` only
writes the record.

## Plan and apply

For each desired resource the engine finds the current one by name:

* missing: **create**;
* present and unmanaged: **adopt** (it becomes managed; update if different);
* present and managed by this sync: **update** or **no change**;
* managed by *another* sync: plan error (a resource has one owner).

Every resource a sync creates or adopts gets a **managedBy marker**
(`gitops_managed`: kind, resource id, sync, applied hash, commit). A managed
resource whose file disappears is **deleted** (`prune` on) or **released**
(`prune` off). Unmanaged resources are never deleted. Deletion protection
(deployments, workflows) blocks the delete; the plan shows why.

Changes made in the UI to a managed resource show up as drift at the next
refresh, and applying restores the git version.

Apply works on the checkout the plan came from; the UI sends that commit, and
a newer commit makes the apply fail with 409 ("refresh and review again").
Each refresh/apply is recorded as a run with its plan and per-resource result.

Status: `in_sync`, `drift` (changes pending), `error` (fetch or plan errors).

## Triggers

* **Refresh** button / `POST /gitops/syncs/{id}/refresh`.
* **Interval** (`intervalSec`, 0 or >= 30): refresh when due; with
  `autoApply` a clean plan is applied immediately.
* **Webhook** `POST /hooks/gitops/{token}`: GitHub/Gitea (`X-Hub-Signature-256`
  / `X-Gitea-Signature` HMAC) or GitLab (`X-Gitlab-Token`) with the sync's
  secret, a push to the sync's branch touching its path. Same evaluator as the
  deployment push webhook.

Git access reuses the deployment helpers: shallow clone with the credential
of a stored `gitFetch` connection, injected via environment, never in the URL.

## Export

`POST /gitops/export` renders current state in the same format, one file per
resource (`deployments/web.yaml`, `alert-rules/cpu.yaml`, ...). Committing an
export unchanged and pointing a sync at it gives an all-"no change" plan.

* `{"format": "zip"}`: download (`kowan-export/...`);
* `{"format": "json"}`: preview;
* `{"push": {"syncId", "branch", "message"}}`: commit under the sync's path on
  `branch` (created from the sync's branch when missing) and push with the
  sync's credential.

No secrets are exported: channels keep their `[[secret]]` refs (sensitive
values without a ref are left out, with a warning), registry passwords and
workflow trigger secrets stay in Kowan.

## API

| | |
|---|---|
| `GET/POST /gitops/syncs`, `GET/PUT/DELETE /gitops/syncs/{id}` | syncs (admin). Create and webhook rotation return `webhookSecret` once. Delete keeps the managed resources. |
| `POST /gitops/syncs/{id}/refresh` | fetch + plan |
| `GET /gitops/syncs/{id}/plan` | latest plan |
| `POST /gitops/syncs/{id}/apply` `{commit}` | apply |
| `GET /gitops/syncs/{id}/runs[/{runId}]` | history |
| `GET /gitops/syncs/{id}/managed` | managed resources |
| `POST /gitops/syncs/{id}/webhook` | rotate token + secret |
| `GET /gitops/kinds`, `GET /gitops/resources`, `POST /gitops/export` | kinds, exportable resources, export |

## Adding a kind

Write a `KindHandler` (see `kowan/gitops/handlers_ext.py`): `normalize`
(validate the git spec into a canonical dict), `current` (Kowan's state in the
same canonical shape), `create` / `update` / `delete`, `defaults` (pruned on
export), `order`. Register it with `register_kind(...)`. Because desired and
current share one shape, diff and export come for free.

Code: `kowan/gitops/` (`resources.py`, `kinds.py`, `handlers.py`,
`handlers_ext.py`, `diff.py`, `engine.py`, `export.py`, `service.py`),
`kowan/store/gitops.py`, `kowan/api/gitops.py`, UI in `web/frontend/src/pages/GitOps.tsx`
and `src/components/gitops/`.

## Limitations

* Renaming a resource in git is a delete + create.
* A channel's resolved secret is refreshed only when the channel itself is
  applied (changing the secret's value is not drift).
* Exporting into a sync's own path next to existing files can define a
  resource twice (a duplicate error) until the old file is removed.
* Plans are per sync; two syncs cannot manage the same resource.
