---
title: Host groups
description: Put hosts in groups such as production, staging or customer-x, then give permissions, schedules, runbooks and auto-update policies to a whole group.
---

A group is a word you give to hosts: `production`, `staging`, `customer-x`. A host can be in several groups. What you set on a group applies to every host in it, including the ones you add to it later.

Set a host's groups on the **Hosts** page, with the **Groups** button of its row. The Groups column shows them and filters the list.

Group names use lowercase letters, digits, dots, dashes and underscores, up to 40 characters.

## What a group is for

| Where | What it does |
| --- | --- |
| [Teams and permissions](users-and-roles.md) | A grant with the scope **Host group** gives its level on every host in the group. |
| [Auto-update](image-updates.md) | **Auto-update → Host groups** sets the default mode of every container and deployment on the group's hosts that has no policy of its own. |
| [Schedules](schedules.md) | The target **Host group** starts, stops or restarts every container on its hosts, for example to stop staging at night. |
| [Runbooks](runbooks.md) | A step on **Group · name** runs on the service of that name on every host in the group. |
| Inventory | **Workloads → More → Export inventory** includes each host's groups. |

## Kowan's own containers

Kowan's server, agents and helpers are never stopped by a group schedule (stopping an agent would cut its host off) and never updated by a group's auto-update policy: they update through **Settings → Server updates**.

A group schedule also skips containers labelled `kowan.schedule.group: "off"`, and **start** wakes only the containers meant to run (restart policy `always` or `unless-stopped`), not one-off jobs.
