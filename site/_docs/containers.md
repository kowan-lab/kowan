---
title: Containers
description: Inspect and operate a single Docker container in Kowan: start, stop and update it, read its logs, open a shell, watch live stats and browse its files.
---

Every container on every host has its own page in Kowan, with what it runs, how it is wired and what it is doing right now. From there you start, stop, update or remove it, follow its logs, open a shell inside it and browse its files, on the local host and on remote hosts alike.

## Open a container

- From a workload: on the **Overview** tab of a [workload](applications.md), open the container from its row in **Containers**.
- From anywhere: press ⌘K (Ctrl+K), type part of the container's name and pick it under **Containers**.

The header shows the container's name, its state, its health when it has a health check, and **Update available** when the registry has a newer image. Below it are the facts that matter most:

| Fact | What it shows |
|---|---|
| **Image** | The image reference, its update status and its vulnerability counts when it has been scanned. |
| **Origin** | The workload the container belongs to: a deployment, a Compose stack, or none. |
| **Host** | The host it runs on. |
| **Health** | The health check status and how many checks in a row have failed. |
| **Restarts** | How many times Docker restarted it, and its restart policy. |
| **Up since** or **Last stopped** | When it started, or when it stopped and with which exit code. |

When the container crashed or is unhealthy, a band under the header explains the problem and shows its last log lines.

## Start, stop and remove

The buttons in the header follow the container's state: **Stop** and **Restart** while it runs, **Start** when it is stopped, **Unpause** when it is paused. The **More** menu holds the rest:

**Pause**
:   Freezes every process in the container until you click **Unpause**.

**Check for image update**
:   Asks the registry whether the image's tag points at a newer image.

**Edit & recreate…**
:   Changes the container's settings (image, ports, environment, volumes, networks, resources and more) by replacing it with a new container of the same name. Saving stops and removes the current container: files written inside it, and anonymous volumes, are lost; named volumes and bind mounts are kept. It is disabled for containers that belong to a deployment or a Compose stack: change those through their deployment or Compose file instead.

**Expose on a domain…**
:   Publishes the container through the host's reverse proxy, with HTTPS. See [Exposure and HTTPS](exposure-and-https.md).

**Kill…**
:   Stops the container at once with SIGKILL, without the grace period **Stop** gives it to shut down cleanly. Use it when a container does not respond to **Stop**.

**Remove…**
:   Deletes the container and its writable layer. Named volumes and bind mounts are kept. A running container needs **Force**, which stops it first. If the container belongs to a deployment or a stack, the next deploy or `compose up` creates it again.

Start, stop, restart, pause, kill and remove are also in the menu on each container's row in a workload.

## Update the image

When a newer image is available, **Update** in the header pulls it and recreates the container with the same configuration. The container gets a new ID, and the page follows it. For a container that belongs to a deployment, **Update** updates the deployment instead, so the next deploy does not undo it.

To check, update or set an automatic update policy, open the **Image** tab. See [Image updates](image-updates.md) and [Vulnerability scanning](vulnerability-scanning.md).

## Tabs

**Overview**
:   How the container runs and how it is wired: **Runtime**, **Ports**, **Mounts**, **Networks**, **Environment** and **Labels**, next to its live **Resources** and the other containers of its **Stack**.

**Logs**
:   The container's output, live. Filter lines by text or by level, follow new lines, show timestamps, wrap long lines, and download what you see. To keep logs and search them later, turn on **Keep logs** for the workload; see [Logs](logs.md).

**Console**
:   A shell (`/bin/sh`) inside the running container, in your browser. The container must be running and its image must include `/bin/sh`.

**Stats**
:   Live CPU (as a share of the whole host, where 100% means every core is busy), memory against the container's limit, network and disk traffic since it started, and the number of processes.

**Files**
:   The container's file system. See [Browse and edit files](#browse-and-edit-files).

**Image**
:   The image reference, the running digest and the registry's, the update policy and the image's vulnerabilities.

**Inspect**
:   The raw output of `docker inspect`, with a button to copy it as JSON.

## Browse and edit files

The **Files** tab works on stopped containers and on images without a shell, on any host, because it goes through the Docker Engine's archive API.

- **Browse** folders, and go to a path directly.
- **View and edit** text files up to 1 MiB, and save them back.
- **Download** a file, or a whole folder as a `.tar` archive.
- **Upload** a file of up to 1 GiB into the current folder.
- **Create** folders.
- **Rename** and **delete** files and folders. These two run `mv` and `rm` inside the container, so it must be running and its image must include them.

A folder with a very large number of entries shows only the first ones, and says so.

## Who can do what

| Action | Viewer | Operator | Admin |
|---|---|---|---|
| See containers, their state, logs and stats | Yes | Yes | Yes |
| List files | Yes | Yes | Yes |
| Start, stop, restart, pause, kill, remove, update | | Yes | Yes |
| Open the console | | Yes | Yes |
| Read, download, upload, edit and delete files | | Yes | Yes |

Viewers see environment variable values masked. Teams can limit all of this to some hosts or workloads; see [Users and roles](users-and-roles.md). Every change is recorded in the [audit log](audit-log.md).

## From the command line

`kowanctl` lists and controls containers. `--host` takes a host's ID and defaults to the local host.

```bash
kowanctl container ls --host <host-id> --all
kowanctl container restart <container-id> --host <host-id>
kowanctl container logs <container-id> --host <host-id>
```

`kowanctl container` also has `start` and `stop`. Run `kowanctl hosts ls` to see host IDs. See [Command line](cli.md).

## Related

- [Applications](applications.md): containers grouped into workloads.
- [Deployments](deployments.md): run a new container or Compose stack.
- [Disk cleanup](disk-cleanup.md): remove stopped containers and unused images.
