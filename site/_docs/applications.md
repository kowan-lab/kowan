---
title: Applications
description: How Kowan groups containers into workloads (deployments, Compose stacks and single containers) and how to find, check and operate them across hosts.
---

Kowan does not make you think in containers. It groups them into **workloads**, the applications you actually run: a Compose stack, a deployment Kowan manages, or a single container on its own. The **Workloads** page lists every workload on every host, with the ones that need attention first.

## Kinds of workload

Kowan builds the list from what Docker reports, so containers and stacks you started outside Kowan appear too, with no setup.

Deployment
:   A workload Kowan manages: a Compose stack or a single container you deployed from Kowan, from an editor, a template or a Git repository. Kowan stores its configuration, keeps every change as a revision and, unless you pause auto-heal, redeploys it when its containers go missing or stop. See [Deployments](deployments.md).

Compose stack
:   Containers that share a Docker Compose project, or a Docker Swarm stack, started outside Kowan. Kowan finds them by the labels Docker Compose and Swarm put on their containers. A Swarm service outside any stack is one workload too.

Single container
:   A container that belongs to no stack and no deployment.

On the **Workloads** page, the **Managed** tab lists deployments and **Not managed** lists everything else. Branch and pull request [previews](previews-and-promotion.md) are deployments, marked as previews.

## Browse workloads

Go to **Workloads**. The host picker at the top of the sidebar sets the scope: one host, or **All hosts**.

- **Tabs:** **All**, **Managed** and **Not managed**.
- **Filters:** **Needs you** (something is wrong), **Updates** (a newer image is available) and **Public** (a port is published on every network interface of the host).
- **View:** **Cards** or **Table**. In the table, expand a workload to see its services and their containers.

In the card view, workloads that need attention come first under **Needs you**, followed by **The rest**. Hosts that are offline are listed at the top, and their workloads appear under **Last known**, showing their last known configuration.

To find a workload quickly from anywhere, press ⌘K (Ctrl+K) and type part of its name.

### Workload states

Each workload shows one state, worked out from its containers:

| State | Meaning |
|---|---|
| **Running** | Every container that should run is running. |
| **Degraded · x/y** | A container crashed, or a Compose service has no container at all. `x/y` counts the replicas up out of those requested in the Compose file. |
| **Unhealthy** | A container's health check is failing. |
| **Restarting**, **Paused**, **Starting** | A container is restarting or paused, or its health check has not passed yet. |
| **Stopped** | Nothing runs, and nothing crashed. |
| **Deploying**, **Deploy failed**, **Not deployed** | A deployment with no containers yet: being deployed, failed to deploy, or never deployed. |
| **Unknown** | The host is offline. |

When a workload needs you, its card says why, such as `backend is missing` or `api exited with code 1`.

A container that exits with an error code counts as crashed, unless it was stopped on purpose. Kowan tells the two apart by the restart policy: Docker restarts a crashed container whose policy is `always` or `unless-stopped`, so one that stays down was stopped. A start error, the policy `no` and an `on-failure` policy that ran out of retries are crashes.

Containers that Swarm replaced during an update are shown as **Replaced** and do not count against the workload.

## Open a workload

Click a workload to open its page. The header shows its state, host, kind, source, what it exposes and when it was created, with actions on the right:

- **View logs**.
- **Actions**: start and stop the whole workload. For a stack or a single container, it also restarts it and opens each container's page. For a deployment, it holds **Deploy** or **Redeploy**, **Auto-heal** and **Deletion protection**, among others; see [Deployments](deployments.md).
- **Update · N**, when N services have a newer image: updates them all at once. See [Image updates](image-updates.md).
- **New versions · N**, for a deployment pinned to version tags: lists newer tags of the same shape.

The page has these tabs:

**Overview**
:   The containers, grouped by service, with their state, image and ports. Each container has buttons for its logs, its console and its page, and a menu to start, stop, restart, pause, kill or remove it. For a deployment, each service has a **Replicas** control. Below, **Resources** charts CPU, memory and network over time (see [Resource history](resource-history.md)), and **Automation & access** sums up the workload's policies.

**Logs**
:   Live logs of every container, and the **Keep logs** setting that stores them so you can search them later. See [Logs](logs.md).

**Configuration**, **Compose** or **Config**
:   For a deployment, the editable configuration. For a stack Kowan does not manage, a read-only view of its services as Docker reports them, with its Compose project and, when Docker reports them, its files and folder on the host. For a single container, its settings.

**History**
:   Deployments only: every revision, with diffs and rollback. See [Revisions and rollback](revisions-and-rollback.md).

**Policies**
:   The workload's automation: **Updates**, **Backups**, **Schedule** and **Exposure**. See [Image updates](image-updates.md), [Volume backups](volume-backups.md), [Schedules](schedules.md) and [Exposure and HTTPS](exposure-and-https.md).

**Activity**
:   What happened to the workload: Docker events, and for a deployment its deploys and changes.

## Manage a stack Kowan does not manage

Kowan shows and operates stacks started elsewhere: you can start, stop and restart them, read their logs and handle each container. It cannot edit them, because it does not have their Compose file.

To manage one fully, import it: **Workloads → New workload → Import stacks** reads stacks from Portainer or a folder of Compose files. A stack imported under its Compose project name takes over the containers that already run. See [Importing stacks](importing.md).

## Who can do what

Viewers see workloads, their state and their logs. Starting, stopping, deploying and every other change need the operator role. Teams can be limited to some hosts and workloads. See [Users and roles](users-and-roles.md).

## Kowan's own containers

The Kowan server, its agents and the short-lived helpers they start (for updates, scans, volume browsing and backups) are hidden from Workloads, the Overview, Logs and search. To show them, an admin turns on **Kowan's own containers** in **Settings → General → Features**.

## Related

- [Containers](containers.md): work with a single container.
- [Deployments](deployments.md): create and change deployments.
- [Container labels](container-labels.md): set policies from a Compose file.
