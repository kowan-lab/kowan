---
title: Previews and promotion
description: Run branches and pull requests as preview environments next to a Git deployment, and promote a tested image or commit from staging to production.
---

Two features help you move a change safely from a branch to production. **Preview environments** run other branches and pull requests of a Git deployment next to it, each as its own deployment. **Promotion** copies what a tested deployment runs, its image or its commit, into another deployment, such as staging into production.

## Preview environments

Previews are available for a Compose [deployment from a Git repository](deployments.md#deploy-from-a-git-repository) that runs on a single host. Each preview is an ordinary deployment on the same host, named after its parent:

- `<deployment>-<branch>` for a branch, for example `shop-feature-checkout`;
- `<deployment>-pr-<number>` for a pull or merge request, for example `shop-pr-42`.

It runs the same Compose file from the same repository, at its own branch, with the parent's stack variables.

### Turn previews on

1. Open the Git deployment. On the **Overview** tab, find the **Preview environments** card.
2. Click **Set up previews**.
3. Fill in the settings and click **Save**.

| Setting | Meaning |
| --- | --- |
| **Branches** | Comma-separated patterns, such as `feature/*, fix/*`. A push to a matching branch (other than the deployment's own) creates a preview. Leave empty for no branch previews. |
| **Pull and merge requests** | Opening a pull or merge request creates a preview, and closing or merging it removes the preview. |
| **Remove idle previews** | Remove a preview nobody pushed to for **1**, **3**, **7** or **30 days**, or **Never**. |
| **At most** | How many previews can run at once, from 1 to 20. Pushes beyond the limit are ignored. |

Previews are driven by the deployment's push webhook, so add it to your Git host first (see [Deploy from a Git repository](deployments.md#deploy-from-a-git-repository)). Kowan reacts to GitHub, Gitea and GitLab events after checking their signature:

- a push to a branch updates every preview of that branch, and creates one when the branch matches **Branches**;
- a pull or merge request being opened, reopened or updated creates or updates its preview;
- deleting a branch, or closing or merging a request, removes its previews.

To start one by hand, click **New preview** and enter a **Branch**. From the command line:

```bash
kowanctl deployment preview <id> feature/checkout   # create or redeploy a branch preview
kowanctl deployment previews <id>                   # list previews
```

### How previews avoid conflicts

Before a preview deploys, Kowan removes from its copy of the Compose file:

- fixed host ports: `"8080:80"` becomes `"80"`, and Docker picks a free host port;
- every `container_name`.

The preview runs under its own Compose project name, so Compose gives its containers, networks and named volumes their own names. Bind mounts, external volumes and volumes with a fixed `name:` are shared with the parent.

To route a preview to its own address, read these variables in the Compose file. Kowan sets them only in previews:

| Variable | Value |
| --- | --- |
| `KOWAN_PREVIEW` | `1` |
| `KOWAN_PREVIEW_NAME` | The preview's Compose project name, such as `shop-pr-42` |
| `KOWAN_PREVIEW_BRANCH` | The branch it runs |
| `KOWAN_PREVIEW_PR` | The pull or merge request number, empty for a branch preview |

For example, a Traefik rule that gives each preview its own host name and keeps the parent's:

```yaml
labels:
  - "traefik.http.routers.${KOWAN_PREVIEW_NAME:-shop}.rule=Host(`${KOWAN_PREVIEW_NAME:-shop}.example.com`)"
```

### Cleanup

Kowan checks every 10 minutes for previews to remove: idle ones past **Remove idle previews**, and those of a deployment whose previews were turned off. Deleting the parent deployment deletes its previews. To remove one yourself, use its remove button in the **Preview environments** list.

## Promotion

Promotion runs in one deployment what another one runs. What moves is the artifact, never the environment: the target keeps its own name, host, environment variables, ports and volumes.

| Source and target | What is promoted |
| --- | --- |
| Container deployments | The image |
| Inline Compose deployments | The image of every service both files have. The target's file is edited in place, with comments and layout kept (when its layout does not allow that, the file is rewritten and the dialog says its comments are gone). Services only in the source are listed, not added. |
| Git deployments of the same repository | The exact commit the source last deployed. The target is pinned to it with **Auto deploy on push** off. |

Kowan refuses to promote between different kinds: a container and a Compose deployment, a Git and an inline deployment, or two different repositories. Previews cannot be promoted, and a Git source that was never deployed has nothing to promote.

### Promote a deployment

1. Open the source deployment, for example `shop-staging`. On the **Overview** tab, find the **Promotion** card.
2. Click **Promote to…** and pick the target in **Promotes to**, for example `shop`. Kowan remembers it.
3. Click **Promote to shop**. The dialog lists each change, any notes, and the diff of the target's configuration.
4. Leave **Deploy shop now** ticked to redeploy the target, or untick it to only save.
5. Confirm with **Promote to shop**.

The promotion is saved as a new revision of the target, so you can undo it from its **History** tab. See [Revisions and rollback](revisions-and-rollback.md). You need the operator role and permission to manage the target deployment.

### From the command line

```bash
kowanctl deployment promote <id> --dry-run       # show what would change
kowanctl deployment promote <id>                 # promote to the saved target and deploy
kowanctl deployment promote <id> --to <target-id> --no-deploy
```

The API offers the same steps: `PUT /api/v1/deployments/{id}/promotion` sets the target, `GET /api/v1/deployments/{id}/promote?to=` returns the plan, and `POST /api/v1/deployments/{id}/promote` applies it. See [REST API](api.md).
