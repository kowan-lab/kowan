---
title: Volume backups
description: Back up Docker volumes with restic to S3, SFTP, a REST server or a local path, on a schedule with retention, then browse snapshots and restore them.
---

Kowan backs up Docker volumes with [restic](https://restic.net): encrypted, deduplicated snapshots to an S3-compatible bucket, an SFTP server, a restic REST server or a folder on a host. Jobs run on a schedule with a retention policy, and you can browse and restore snapshots from the console.

These are backups of your applications' data. To back up Kowan itself, see [Backup and restore](backup-and-restore.md).

## How it works

- Each backup runs a short-lived `restic/restic:0.17.3` container on the volume's host, through that host's Docker connection, so volumes on [remote servers](adding-servers.md) are backed up the same way.
- The volumes are mounted read-only. Each volume gets its own snapshot, tagged with the volume and the job.
- A repository is initialized the first time it is used.
- Only named volumes are backed up. Bind mounts are not.

## Add a repository

A repository is where restic stores snapshots. Only admins can add, edit or delete one.

1. Go to **Automation → Backups** and click **Repository**.
2. Enter a **Name** and pick a **Type**:

    | Type | Fields |
    | --- | --- |
    | Local path on a host | **Host**, **Path on the host** (absolute, created if missing) |
    | S3-compatible (AWS, MinIO, R2, B2…) | **Endpoint** (empty for AWS, `http://` for a plain MinIO), **Region**, **Bucket**, **Prefix**, **Access key ID**, **Secret access key** |
    | SFTP | **Host**, **Port** (22), **User**, **Path**, **SSH private key** (OpenSSH format, without a passphrase) |
    | restic REST server | **URL**, **Username**, **Password** |

3. Set the **Repository password**. It encrypts every snapshot.
4. Optionally set a **Docker network** for the restic container to join, for example to reach a MinIO container by name.
5. Save, then click **Test** to check that restic can open or initialize the repository.

!!! danger "Keep a copy of the repository password"
    Without the repository password, nobody can decrypt the snapshots, Kowan included. Store it somewhere outside Kowan.

Credentials are stored encrypted with Kowan's secret key and never shown again. Leave a secret field blank when you edit a repository to keep its stored value. A local repository can only be used by jobs on its own host. For SFTP, the server's host key is accepted on first use.

## Create a backup job

A job says which volumes to back up, where, when, and how many snapshots to keep. Operators can create and run jobs.

1. On **Automation → Backups**, click **New job**.
2. Enter a **Name**, then pick the **Host** and the **Repository**.
3. Under **Volumes**, choose how to **Select by**:

    | Option | Backs up |
    | --- | --- |
    | Volume names | The volumes you pick |
    | Deployment | The named volumes mounted by a deployment's containers |
    | Compose stack | The volumes of a Compose project's containers and the volumes labeled with the project |
    | Label `kowan.backup=true` | Volumes with that label, and the volumes mounted by containers with that label |

    Kowan resolves the selection again at every run, so new volumes are picked up. **Matches now** shows what it matches today.

4. Under **Schedule and retention**, set a **Schedule (cron)** and a **Timezone**. Leave the schedule empty for a job you only run by hand.
5. Set how many snapshots to keep: **Last**, **Daily**, **Weekly**, **Monthly** and **Yearly**. After each backup, restic removes the older snapshots of this job, volume by volume. Leave every value at 0 to keep everything.
6. Under **Hooks**, choose what happens around the backup (see [Keep databases consistent](#keep-databases-consistent)).
7. Add **Tags** for every snapshot if you want, leave **Enabled** on, and save.

A run that is more than 10 minutes late, because the server was down for example, is skipped rather than run late. The same rule applies to [schedules](schedules.md).

### Use labels to choose volumes

With **Select by** set to **Label `kowan.backup=true`**, you mark what to back up in your Compose files:

```yaml
services:
  db:
    image: postgres:17
    labels:
      kowan.backup: "true"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

### Keep databases consistent

Copying the files of a running database can capture them mid-write. A job has two ways to avoid that:

- **Stop containers using these volumes during the backup** stops the running containers that mount the volumes and starts them again afterwards, even when the backup fails.
- **Before backup** and **After backup** run a command in a running container, for example `pg_dump` into a file on the volume. A failing before command cancels the backup; a failing after command turns the run into a warning.

## Back up from a workload or a volume

You don't have to start from the Backups page:

- On a workload, the **Policies** tab has a **Backups** section. When no job covers the workload yet, turning the knob **On** opens a new job for its volumes, with the containers stopped during the backup. Otherwise the knob enables or disables the jobs that cover it.
- On a volume's page, the **Backups** section lists its snapshots. **Back up now** takes a one-off snapshot into the chosen repository (no retention is applied), and the menu offers **New scheduled job for this volume**.

## Runs and alerts

The **Run history** tab lists every run with its status and log.

- A run where restic could not read some files still writes a snapshot and is marked as a warning.
- Logs have every secret value redacted and are capped at 256 KiB. Runs are kept for 180 days.
- A failed run raises a critical alert in the bell and on your [notification channels](alerts-and-notifications.md). The next successful run of the job resolves it. Failed backups also appear in the [digest](digest.md).

## Restore a volume

1. Open the volume's page and find the snapshot in **Backups**.
2. Click **Browse** to look inside it first, one folder at a time (up to 5000 entries per folder).
3. Click **Restore** and choose where:
    - **Into a new volume**: safe, nothing existing is touched. Restoring is refused if a volume with that name exists.
    - **Into** *the volume* **(overwrite)**: replaces the volume's content with the snapshot. You can **Stop containers using the volume, start them after**, and choose an **Exact restore** that also deletes files added since the snapshot.
4. Click **Restore**.

!!! tip
    Restore into a new volume first and check its content before you overwrite a live volume.

## Limits

- Repository credentials reach the restic container as environment variables, so anyone who can run `docker inspect` on that host can read them while the container runs. It is removed when the operation ends.
- Only named volumes are backed up.
- The snapshot size shown is the size restic processed, before deduplication.

Everything on this page is also available under `/api/v1/volbackups`. See [REST API](api.md).
