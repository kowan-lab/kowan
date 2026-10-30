---
title: Upgrading
description: Update the Kowan server and its agents from the console with automatic rollback, roll back by hand, or upgrade manually with Docker.
---

Kowan updates itself and its agents from the console. Each update tests the new image before it touches anything, keeps the previous version aside, and restores it automatically if the new one does not come up healthy.

## Know when a release is out

The server checks the published Kowan releases every 6 hours, starting 2 minutes after it starts. The first time it sees a newer release than its own, it posts **Kowan X.Y.Z is available** to the bell in the top bar, once per version.

**Settings → General → Server updates** shows the version that is **Running**, the **Latest release** and the **Previous** version kept for rollback. Click **Check** to ask the registry again right away.

| Variable | Default | Purpose |
|---|---|---|
| `KOWAN_RELEASE_REPOSITORY` | `ghcr.io/kowan-lab/kowan` | The image repository whose version tags are the releases. Empty or `off` turns the release check off. |

Release images are published as `ghcr.io/kowan-lab/kowan` (server) and `ghcr.io/kowan-lab/kowan-agent` (agent), with three kinds of tag: the exact version (`0.1.12`), the minor version (`0.1`) and `latest`. The [changelog](changelog.md) lists what changed in each release.

## Update the server

Server updates are for admins.

1. Go to **Settings → General → Server updates**.
2. Click **Update to X.Y.Z** (or **Update now** when a newer image is available for the same tag).
3. Check the **New image**. To install a specific version instead, enter it in **Another image (optional)**, such as `ghcr.io/kowan-lab/kowan:0.1.12`.
4. Click **Start update**.

The console is unavailable for a few seconds while the server restarts, then the page reconnects on its own and shows the result.

### Which image an update installs

When you leave **Another image** empty, the image depends on the tag the server runs:

- A server pinned to a release, such as `kowan:0.1.11`, moves to the newest release's tag, such as `kowan:0.1.12`.
- Any other tag (`latest`, `0.1` or a tag of your own) is pulled again and updated in place.

### What happens during an update

1. **Pull.** Kowan pulls the new image and checks that it matches the host's operating system and architecture. If the registry cannot be reached but the image is already on the host, it uses that. Updating to the image already running stops here with "already up to date".
2. **Snapshot.** Kowan saves a copy of the database and its keys in the data volume.
3. **Smoke test.** A throwaway container from the new image checks that it starts, reads the configuration and can migrate a copy of the database, within 60 seconds. If it fails, the update stops and nothing has changed.
4. **Swap.** A short-lived helper container, started from the current image, stops the server, renames it `<name>-previous`, and creates the new container with the same name and configuration.
5. **Health check.** The new container must stay running without restarts for 10 seconds and report itself healthy, within 120 seconds.
6. **Finish or roll back.** On success the previous container stays stopped, kept for a manual rollback. On failure, Kowan stops and removes the new container, restores the database snapshot and starts the previous version again.

Each step appears in the panel while it runs, and every update is kept in the history below it, with its result and duration. Click an entry for its steps and, after a failure, the last log lines of the new version.

| Result | Meaning |
|---|---|
| **updated** | The new version runs and passed its health check. |
| **aborted** | Stopped before changing anything, for example when the pull or the smoke test failed. |
| **rolled back** | The new version failed, and the previous one runs again. |
| **failed** | Even the rollback failed. Both containers are kept and the update's log lists the recovery steps. |
| **unknown** | An agent did not reconnect within 10 minutes of its update, so Kowan cannot tell the result. |

### When self-update is not available

The panel says **Self-update is not available for this server**, with the reason, when the server:

- does not run in a container, for example when you run it from source;
- has no Docker socket mounted;
- keeps its data directory outside a volume or bind mount, so a new container would lose the database;
- cannot find its own container. Set `KOWAN_SERVER_CONTAINER` to the container's name.

In those cases, [upgrade manually](#upgrade-manually).

## Update the agents

Agent updates work the same way, on the agent's host: pull, smoke test, swap and health check, with an automatic rollback. An agent counts as healthy while its edge connection is up or, in direct mode, while it is listening for the server.

To update one agent, open its host from **Settings → Hosts & agents** and use the update panel on its page.

To update several at once:

1. Go to **Settings → Hosts & agents** and click **Update agents**.
2. Tick the hosts to update. Each row says whether an update is available.
3. Click **Update N agents**.

All the selected agents update at the same time, each with its own progress bar. One that fails rolls back alone while the others carry on. While an agent restarts, its host is offline for a few seconds; Kowan waits for it to come back instead of treating its deployments as failed.

Update the agents right after the server, so every host runs the same release. Operators see an agent's update status; only admins can update or roll it back.

## Roll back

The previous version stays on the host after a successful update, so you can return to it if the new one misbehaves later.

1. Open **Settings → General → Server updates**, or the agent's host page.
2. Click **Roll back**.
3. For the server, keep **Restore the database as it was before the last update** ticked, unless you know the previous version can read the current database.
4. Confirm with **Roll back**.

Kowan switches to the previous container with its original configuration. If it does not come up healthy, the current version is restored. A rollback can itself be rolled back.

!!! warning "Restoring the database discards recent changes"
    With the database restore ticked, everything written since that update is undone. The discarded database is not deleted: it is moved to `backups/rolled-back-*` in the data volume.

## Upgrade manually

When self-update is not available, or you prefer to do it yourself, replace the container with the new image. Your data stays in the `kowan-data` volume. Download a [backup](backup-and-restore.md) first.

With `docker run`:

```bash
docker pull ghcr.io/kowan-lab/kowan:latest
docker stop kowan && docker rm kowan
docker run -d --name kowan --restart unless-stopped \
  -p 5081:5081 \
  -v kowan-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/kowan-lab/kowan:latest
```

Repeat the same options and environment variables you started it with.

With Docker Compose, from the folder that holds `docker-compose.yml`:

```bash
docker compose pull
docker compose up -d
```

To upgrade an agent by hand, remove its container and run its join command again: the host keeps its token. Show the command from the host's row in **Settings → Hosts & agents**.

```bash
docker pull ghcr.io/kowan-lab/kowan-agent
docker rm -f kowan-agent
```

If you run the agent with Docker Compose, run `docker compose pull` and `docker compose up -d` on its host instead.

## Limits and recovery

- An update never removes old images: the container kept for rollback still uses the previous one.
- Kowan keeps the last 3 database snapshots taken before updates, in `backups/` in the data volume, and the last 50 update records per server or agent.
- During an update, Docker briefly shows the helper container as unhealthy. This is expected and harmless.

### If the host reboots during an update

If the host reboots in the few seconds between stopping the old container and creating the new one, nothing can finish the update: the old container is left stopped as `<name>-previous`, with no restart policy. Bring it back by hand. For a server container named `kowan`:

```bash
docker rename kowan-previous kowan
docker update --restart unless-stopped kowan
docker start kowan
```

For an agent, use `kowan-agent-previous` and `kowan-agent`.

## Related

- [Backup and restore](backup-and-restore.md)
- [Adding servers](adding-servers.md)
- [Configuration](configuration.md)
