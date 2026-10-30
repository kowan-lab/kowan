---
title: Deployments
description: Create container and Docker Compose deployments in Kowan from an editor or a Git repository, then deploy, redeploy, stop, protect and delete them.
---

A deployment is a container or a Docker Compose stack whose configuration Kowan owns. Kowan deploys it to one host, saves every change as a [revision](revisions-and-rollback.md) you can roll back, and redeploys it when it goes down. Containers and stacks started by other tools still appear in [Workloads](applications.md), but only deployments get the full set of features on this page.

## Deployment types

Every deployment has a type and a place where it runs.

| Type | What it runs | Where the definition comes from |
| --- | --- | --- |
| **Container** | One image, with its ports, environment, volumes and other `docker run` settings | The form |
| **Compose** | A stack of services, like `docker compose up` | An inline YAML file, or a file in a Git repository read on every deploy |

**Single host**
:   The deployment runs on one Docker host, picked in the **Host** field.

**Swarm cluster**
:   The deployment runs as a Swarm stack (`docker stack deploy`) on one Swarm manager, with the number of **Replicas** you set.

A deployment always targets exactly one host or one Swarm manager, because volumes and bind mounts live on a single host. To run the same application on two servers, create two deployments; [promotion](previews-and-promotion.md) copies the image or commit one runs into the other.

## Create a deployment

You need the operator or admin role. See [Users, roles and teams](users-and-roles.md).

1. Open **Workloads → New workload** and choose **Compose or container deployment**, or **From a git repository**.
2. Enter a **Name**. It must be unique, and for a Compose deployment it also becomes the Compose project name (lowercased, with unsupported characters replaced by `-`).
3. Choose the **Type** and **Where it runs**, then the **Host**.
4. Describe what runs:
    - For a container: the **Image**, **Network ports**, **Environment variables** and, if needed, the **Advanced container settings**.
    - For Compose: paste or write the file under **Inline YAML** (the **Easy** editor builds simple services for you, **YAML** shows the file), or choose **Git repository**. Kowan checks the file as you type; see [Compose validation](compose-validation.md).
5. Under **After creating**, choose what to set up with it (see below).
6. Click **Create**. The deployment's page opens.

### After creating

These settings are the same ones you find later on the deployment's page. They are applied as soon as the deployment is saved.

| Setting | What it does |
| --- | --- |
| **Deploy** | **Deploy right after creating**. Turn it off to save the deployment stopped and deploy it later. |
| **Updates** | The [image update](image-updates.md) mode (**Off**, **Notify**, **Pull** or **Auto**) and an optional maintenance window. |
| **Backups** | A daily [volume backup](volume-backups.md) to a repository you already added, with how many daily snapshots to keep. |
| **Schedule** | Start and stop the deployment at set times, on weekdays or every day. See [Schedules](schedules.md). |
| **Domain** | Publish a service on a domain with HTTPS through the host's Traefik. See [Exposure and HTTPS](exposure-and-https.md). |
| **Webhook** | **Create a redeploy webhook** that CI can call. |
| **Protection** | **Protect from deletion** until protection is turned off. |

## Deploy from a Git repository

For a Compose deployment, choose **Git repository** under **Compose** and fill in:

| Field | Meaning |
| --- | --- |
| **Repository URL** | An `https://`, `ssh://` or `git@host:` clone URL. Kowan makes a shallow clone on the server for every deploy. |
| **Branch or tag** | The ref to deploy. Default `main`. |
| **Compose file path** | Relative to the repository root. Default `docker-compose.yml`. A `.env` file next to it is applied. |
| **Credential** | A stored Git connection (GitHub, GitLab, Bitbucket or git-http) for a private repository. Create one in **Settings → Credentials**. |
| **Auto deploy on push** | Redeploy when your Git host reports a push to this ref that touches the Compose file's folder. |

A Git deployment gets its webhook when you create it. On the deployment's **Overview** tab, the **Redeploy webhook** card shows the **URL** and the **Push secret**. Add them to your repository's webhook settings:

- **GitHub or Gitea**: the URL as payload URL, content type `application/json`, and the push secret as secret (Kowan checks the HMAC SHA-256 signature).
- **GitLab**: the URL, and the push secret as secret token.

With auto deploy off, use **More → Pull latest commit**: Kowan looks up the newest commit on the branch and redeploys only if it changed.

## Stack variables and secrets

A Compose deployment has **Stack variables**: they fill the file's `${VAR}` references when it deploys, over a repository's `.env`. A container deployment has **Environment variables**. In both, each row is **Config** or **Secret**:

**Config**
:   Stored as written. It can reference a shared secret from **Settings → Secrets** as `[[name.field]]`.

**Secret**
:   Stored encrypted in a secret the deployment owns, named `<deployment>-env`. The value never appears in the deployment, its revisions or the API, and the form shows it as unchanged until you type a new one. Only an admin can reveal it, on the Secrets page.

Secret values are not part of the revision history: a rollback keeps the current value.

## Change a deployment

Open the deployment, edit it on the **Configuration** tab, then click **Save** or **Save and redeploy**. Viewers see the configuration read-only: environment values, the webhook and credentials in Git URLs are hidden, and a Compose file shows only each service's image, ports, dependencies, restart policy, networks and volumes.

When the deployment is running and auto-heal is on, saving a change to what it runs (image, Compose file, host, environment or secret values) redeploys it in the background. A stopped deployment, or one with auto-heal off, only saves the change until you deploy it.

## Deployment actions

The deployment's header holds **Deploy** (or **Redeploy**), **Stop** or **Start**, and a **More** menu:

| Action | What it does |
| --- | --- |
| **Deploy** / **Redeploy** | Creates or recreates the containers from the saved configuration. Up to three deployments deploy at once; the others wait as pending. |
| **Stop** / **Start** | **Stop** stops a container deployment's containers, takes a Compose project down (its containers are removed, its volumes kept) and removes a Swarm stack. **Start** brings it back without fetching a new commit. |
| **Pull latest commit** | Git deployments only: redeploys if the branch has a new commit. |
| **Start existing containers** | Shown after a failed deploy: starts the containers already on the host. |
| **Auto-heal** | On by default. Every 5 seconds, Kowan checks that each running deployment is still up, and redeploys it when a container or Compose service is missing or stopped, at most once every 2 minutes per deployment. It leaves alone a deployment you stopped, one whose last deploy failed and one whose host is unreachable. |
| **Deletion protection** | Blocks deleting the deployment, from the UI, the API and [GitOps](gitops.md). |
| **Remove containers…** | Removes the containers (and, for Compose, its networks) from the host. The configuration, webhook and history stay, and **Deploy** brings it back. Named volumes are kept. |
| **Delete deployment…** | Removes the containers, then the deployment, its webhook and its [preview environments](previews-and-promotion.md). Volumes are kept. |

## Redeploy webhook

Any deployment can have a webhook: a secret URL that redeploys it with the saved configuration, without signing in. Create it on the **Overview** tab in the **Redeploy webhook** card (**Create webhook**), then call it from CI:

```bash
curl -X POST https://kowan.example.com/hooks/deploy/<token>
```

Kowan answers `202` and deploys in the background. **Rotate** issues a new URL (and, for a Git deployment, a new push secret) and stops the old one at once. **Remove** turns the webhook off. For more ways to react to events, see [Workflows and webhooks](workflows-and-webhooks.md).

## From the command line

`kowanctl deployment` manages deployments from scripts. See [Command line](cli.md).

```bash
kowanctl deployment ls                      # list deployments
kowanctl deployment create -f web.json      # create from a JSON file
kowanctl deployment deploy <id>             # also: redeploy, stop, start
kowanctl deployment status <id>             # per-host state
kowanctl deployment destroy <id>            # remove containers, keep the deployment
kowanctl deployment rm <id>                 # remove containers and delete it
```

The JSON file uses the same fields as the [REST API](api.md):

```json
{
  "name": "web",
  "type": "container",
  "strategy": "standalone",
  "targets": ["local"],
  "spec": { "image": "nginx:1.27" }
}
```

`local` is the host Kowan runs on; use a host's ID for any other server.
