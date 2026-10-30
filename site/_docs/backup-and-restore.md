---
title: Backup and restore
description: Back up the Kowan server's database, keys and Compose stacks from the console or kowanctl, and restore them on the same or a new server.
---

Everything Kowan knows lives in its data volume: users, hosts, deployments and their history, settings, encrypted secrets and the keys that unlock them. A Kowan backup is one `.tar.gz` file with all of it, which you can download from the console or the command line and restore on the same server or a new one.

A Kowan backup covers Kowan itself, not your applications' data. To back up the volumes of your containers, use [volume backups](volume-backups.md).

## What a backup contains

| Item | What it is |
|---|---|
| The database | A consistent copy of Kowan's database, taken while the server runs. |
| `secret.key` | The key that encrypts stored secrets, when Kowan keeps it as a file in the data volume (the default). |
| `jwt.key` | The key that signs sign-in sessions, when Kowan keeps it as a file (the default). |
| `stacks/` | The Compose files and working data of your deployments. |

Other files in the data volume are not included, such as the database of [retained logs](logs.md) and the snapshots taken before [updates](upgrading.md).

!!! danger "A backup holds the keys to your secrets"
    With `secret.key` inside, anyone who has the archive can decrypt every secret stored in Kowan: registry passwords, API tokens, environment values marked secret. Store backups as carefully as the server itself.

If you set the keys with `KOWAN_SECRET_KEY` and `KOWAN_JWT_SECRET` instead of letting Kowan generate them, they have no file and are not in the backup. Keep them yourself, and give the server you restore to the same values: without the right `KOWAN_SECRET_KEY`, stored secrets cannot be decrypted.

## Download a backup

Only admins can download a backup.

1. Go to **Settings → General** and scroll to **Backup & Restore**.
2. Click **Download backup**.

The browser saves a file named `kowan-backup-<date>.tar.gz`.

From the command line, `kowanctl backup` saves the same archive in the current folder, or where `--out` says:

```bash
kowanctl backup --out /backups/kowan-backup.tar.gz
```

Over the API, `GET /api/v1/backup` returns the archive. See [API](api.md).

## Back up on a schedule

There are two ways to take backups automatically.

**From another machine.** Run `kowanctl` from cron on any machine that can reach Kowan, with an [API key](api-keys.md) that has the admin role. The archives land off the server, where they are safe if the server is lost:

```bash
# /etc/cron.d/kowan-backup
0 3 * * * root KOWAN_SERVER=https://kowan.example.com KOWAN_API_KEY=<admin-key> kowanctl backup --out /backups/kowan-$(date +\%F).tar.gz
```

**With a workflow.** In **Workflows**, create a workflow with a **Backup** step and a **Cron schedule** trigger, such as `0 3 * * *`. Each run writes a timestamped `kowan-backup-*.tar.gz` to `backups/` in the data volume, or to the server-side folder you enter in the step's **Path**. See [Workflows and webhooks](workflows-and-webhooks.md).

Kowan does not delete old archives in either case: remove them on your own schedule. A backup that stays in the data volume does not survive the loss of that volume, so copy it elsewhere too.

## Restore a backup

A restore replaces the server's current data with the backup's. It does not happen at once: Kowan checks the archive, stages it in the data volume and applies it the next time the server starts, before it opens the database.

1. Go to **Settings → General → Backup & Restore**.
2. Under **Restore from backup**, choose the archive in **Select a .tar.gz backup**.
3. Click **Restore backup**. Kowan says when the restore is staged.
4. Restart the server, for example with `docker restart kowan`.

From the command line:

```bash
kowanctl restore kowan-backup-2026-10-30.tar.gz
docker restart kowan
```

Over the API, send the archive to `POST /api/v1/restore` as a multipart `file` field or as the request body.

When the server starts, it replaces the database with the backup's, and the keys and the `stacks/` folder too when the backup has them. After the restart, sign in with an account from the backup.

!!! warning
    Everything changed since the backup was taken is lost: new users, deployments, revisions, settings and secrets.

Kowan refuses an archive that is not a Kowan backup or has no database in it, and the current data stays as it was. Uploading another archive replaces a restore that is staged and not yet applied.

## Move Kowan to a new server

1. [Download a backup](#download-a-backup) from the old server.
2. [Install Kowan](installation.md) on the new server, with the same `KOWAN_SECRET_KEY` and `KOWAN_JWT_SECRET` if you set them on the old one.
3. Create a temporary administrator on the setup screen. The restore replaces it.
4. [Restore the backup](#restore-a-backup) and restart the new server.
5. Stop the old server, so the two do not manage the same hosts.

Agents keep their tokens, since those are in the database. If the server's address changed:

- **Edge agents** connect to the address in their `KOWAN_SERVER_URL`. Show each host's join command on the new server and run it again on that host, after `docker rm -f kowan-agent`.
- **Direct agents** are reached at the address stored in Kowan, so nothing changes on their side, except `KOWAN_AGENT_ALLOW_CIDRS` if you limited it to the old server's address.

Set `KOWAN_EXTERNAL_URL` on the new server to its own address. See [Adding servers](adding-servers.md).

## Copy the whole data volume

You can also back up the `kowan-data` volume itself, which includes everything in it. Stop the server first, so that the database is not written during the copy:

```bash
docker stop kowan
docker run --rm -v kowan-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/kowan-data.tar.gz -C /data .
docker start kowan
```

To restore it, for example on a new server, extract the archive into a new, empty `kowan-data` volume before you start Kowan with its usual [installation](installation.md) command:

```bash
docker volume create kowan-data
docker run --rm -v kowan-data:/data -v "$PWD":/backup alpine \
  tar xzf /backup/kowan-data.tar.gz -C /data
```

## Related

- [Upgrading](upgrading.md): every server update snapshots the database first and can roll back.
- [Volume backups](volume-backups.md): back up your containers' data with restic.
- [Security](security.md): how Kowan stores secrets and keys.
