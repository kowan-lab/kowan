---
title: Command line
description: Install kowanctl, sign in, and manage Kowan hosts, containers, deployments, workflows, users, API keys and backups from your terminal or scripts.
---

`kowanctl` is the command-line client for Kowan. It talks to your Kowan server over the [REST API](api.md), so it can do whatever your account or API key is allowed to do, from a terminal or a script.

## Install

`kowanctl` needs Python 3.10 or later. Install it with pip:

```bash
pip install git+https://github.com/kowan-lab/kowan
```

Check that it works:

```bash
kowanctl version
```

## Sign in

Point `kowanctl` at your server and sign in with your Kowan username and password:

```bash
kowanctl --server https://kowan.example.com login
```

`kowanctl` asks for the username and password (or takes them from `--username` and `--password`), then saves the server URL and a sign-in token to `~/.kowan/config.yaml`. Every later command uses them:

```bash
kowanctl hosts ls
```

A sign-in token is valid for 24 hours. When it expires, run `kowanctl login` again.

!!! tip "Use an API key for scripts"
    A token expires and belongs to a person. For scripts, CI and long-running jobs, create an [API key](api-keys.md) with the lowest role that does the job, and pass it with `--api-key` or `KOWAN_API_KEY`. If you sign in to Kowan through single sign-on, an API key is also how you use `kowanctl`.

## Configuration

`kowanctl` needs a server URL and a credential: a sign-in token, an API key, or both. It reads each one from a flag, an environment variable or the config file, and uses the first one it finds in this order:

| Setting | Flag | Environment variable | Config file key |
|---|---|---|---|
| Server URL | `--server` | `KOWAN_SERVER` | `server` |
| Sign-in token | `--token` | `KOWAN_TOKEN` | `token` |
| API key | `--api-key` | `KOWAN_API_KEY` | `apiKey` |

A flag beats an environment variable, which beats the config file, so a variable can override the saved settings for one shell. When both a token and an API key are available, the server authenticates the API key.

The config file is YAML. `kowanctl` creates it readable only by you (mode `0600`), since it holds credentials:

```yaml
server: https://kowan.example.com
token: eyJhbGciOiJIUzI1NiIs...
apiKey: pk_...
```

`kowanctl login` rewrites `server` and `token` and keeps an `apiKey` you added.

Global flags go before the command:

```bash
kowanctl --server https://kowan.example.com --api-key pk_... deployment ls
```

!!! note
    `kowanctl` does not verify the server's TLS certificate, so it works with self-signed certificates. Use it over networks you trust.

### Errors and exit status

On failure, `kowanctl` prints `error: ` and the reason on standard error and exits with status `1`. Errors from the server include the request and the HTTP status, such as `error: GET /api/v1/hosts: invalid token (401)`.

## Commands

| Command | What it does |
|---|---|
| [`login`](#login) | Sign in and save the token. |
| [`hosts`](#hosts) | List and rename servers. |
| [`container`](#container) | List, start, stop and restart containers, and follow their logs. |
| [`image`, `volume`, `network`](#image-volume-network) | List images, volumes and networks on a host. |
| [`deployment`](#deployment) | Create, deploy, roll back, preview and promote deployments. |
| [`workflow`](#workflow) | List and run workflows. |
| [`user`](#user) | List users. |
| [`apikey`](#apikey) | Create, list and revoke API keys. |
| [`backup`, `restore`](#backup-restore) | Download a backup of the server and stage a restore. |
| [`mcp`](#mcp) | Bridge AI assistants that only start local processes to Kowan. |
| `version` | Print the `kowanctl` version. |

Each command needs the role of the API call behind it. Viewers can list hosts, containers, images, volumes, networks, deployments and workflows. Operators can also act on containers, deployments and workflows, and see workflow runs. Admins can also rename hosts and manage users, API keys and backups. See [Users, roles and teams](users-and-roles.md).

In every group, `list` also works for `ls`, and `delete` for `rm`.

### login

```bash
kowanctl --server https://kowan.example.com login [--username NAME] [--password PASSWORD]
```

| Option | Description |
|---|---|
| `-u`, `--username` | Your username. Asked for when omitted. |
| `-p`, `--password` | Your password. Asked for without echo when omitted. |

### hosts

| Command | Description |
|---|---|
| `kowanctl hosts ls` | List servers with their ID, name, type, address, whether they are online and when they were last seen. |
| `kowanctl hosts rename HOST NAME` | Rename a server. `HOST` is its ID or current name. Only the displayed name changes: the ID, deployments and history stay attached. |

### container

Container commands work on one host. `--host` takes the host's ID, as `kowanctl hosts ls` shows it, and defaults to `local`, the server Kowan runs on.

| Command | Description |
|---|---|
| `kowanctl container ls [--host ID] [-a]` | List running containers. `-a`, `--all` also lists stopped ones. |
| `kowanctl container start CONTAINER [--host ID]` | Start a container. |
| `kowanctl container stop CONTAINER [--host ID]` | Stop a container. |
| `kowanctl container restart CONTAINER [--host ID]` | Restart a container. |
| `kowanctl container logs CONTAINER [--host ID]` | Print the container's recent logs and follow new lines until you press Ctrl+C. |

```bash
kowanctl container ls --host 3f2c9a1e-... --all
kowanctl container restart 9b1d2c3e4f5a --host 3f2c9a1e-...
```

### image, volume, network

| Command | Description |
|---|---|
| `kowanctl image ls [--host ID]` | List images with their tag, size and creation date. |
| `kowanctl volume ls [--host ID]` | List volumes with their driver and mount point. |
| `kowanctl network ls [--host ID]` | List networks with their driver and scope. |

`--host` defaults to `local`.

### deployment

`ID` is the deployment ID, as `kowanctl deployment ls` shows it. See [Deployments](deployments.md).

| Command | Description |
|---|---|
| `kowanctl deployment ls` | List deployments with their type, strategy, number of targets and status. |
| `kowanctl deployment create -f FILE` | Create a deployment from a JSON file. It is saved, not deployed. |
| `kowanctl deployment deploy ID` | Deploy to its targets. |
| `kowanctl deployment redeploy ID` | Deploy again. |
| `kowanctl deployment start ID` | Start a stopped deployment. |
| `kowanctl deployment stop ID` | Stop its running containers. |
| `kowanctl deployment status ID` | Show the state of each target. |
| `kowanctl deployment destroy ID` | Remove its running containers and keep the deployment. |
| `kowanctl deployment rm ID` | Remove its running containers and delete the deployment, with its previews. Refused while deletion protection is on. |
| `kowanctl deployment history ID` | List its [revisions](revisions-and-rollback.md), newest first. `*` marks the one running now. |
| `kowanctl deployment diff ID REVISION [--against N]` | Show what a revision changed, as a line diff of its YAML. Compares with the previous revision unless you pass `--against`. |
| `kowanctl deployment rollback ID REVISION [--no-deploy]` | Restore a revision as a new one and deploy it. `--no-deploy` only saves it. |
| `kowanctl deployment previews ID` | List the branch and pull request [previews](previews-and-promotion.md) of a Git deployment. |
| `kowanctl deployment preview ID BRANCH` | Create the preview of a branch, or deploy it again. |
| `kowanctl deployment promote ID [--to ID] [--dry-run] [--no-deploy]` | Run in another deployment what this one runs: its images or its Git commit. |

`promote` prints each change first. `--to` picks the target (by default, the deployment this one promotes to), `--dry-run` stops after the list of changes, and `--no-deploy` saves the target without deploying it.

The file for `create` holds the deployment as JSON. A container deployment on the local host:

```json
{
  "name": "whoami",
  "type": "container",
  "strategy": "standalone",
  "targets": ["local"],
  "spec": {
    "image": "traefik/whoami:latest",
    "ports": [{ "hostPort": 8080, "containerPort": 80, "protocol": "tcp" }]
  }
}
```

```bash
kowanctl deployment create -f whoami.json
kowanctl deployment deploy 7c0e4b8a-...
```

For a Compose stack, set `"type": "compose"` and put the Compose file in `"compose"` as a string.

### workflow

| Command | Description |
|---|---|
| `kowanctl workflow ls` | List workflows with their number of steps and triggers and whether they are enabled. |
| `kowanctl workflow run ID` | Start a run now and print its ID. The run continues on the server. |
| `kowanctl workflow runs ID` | Show the workflow's run history. |

See [Workflows and webhooks](workflows-and-webhooks.md).

### user

| Command | Description |
|---|---|
| `kowanctl user ls` | List users with their role. Admins only. |

### apikey

| Command | Description |
|---|---|
| `kowanctl apikey ls` | List API keys: name, prefix, role, creation date and last use. Secrets are never shown again. |
| `kowanctl apikey create NAME [--role ROLE]` | Create a key and print its secret once. `--role` is `admin`, `operator` or `viewer` (the default). |
| `kowanctl apikey rm ID` | Revoke a key. `revoke` works too. |

```bash
kowanctl apikey create ci-deploy --role operator
```

Store the secret when it is printed: you cannot see it again. To limit a key to a team or to specific hosts, create it in the console. See [API keys](api-keys.md).

### backup, restore

| Command | Description |
|---|---|
| `kowanctl backup [-o FILE]` | Download a backup of the server's database and Compose stacks. The default file is `kowan-backup-<date>.tar.gz` in the current directory. |
| `kowanctl restore FILE` | Upload a backup. The server stages it and applies it at its next restart. |

Both need an admin. See [Backup and restore](backup-and-restore.md).

### mcp

```bash
kowanctl mcp [--url URL] [--api-key KEY]
```

Runs a local MCP server on standard input and output and forwards every message to your Kowan server. It is for AI assistants that start MCP servers as local processes, such as Claude Desktop. See [AI assistants](mcp.md).

| Option | Description |
|---|---|
| `--url` | Kowan server URL. Defaults to `KOWAN_URL`, then the server from the usual configuration. |
| `--api-key` | API key. Defaults to `KOWAN_API_KEY`, then the API key from the usual configuration, then your sign-in token. |
