---
title: Introduction
description: What Kowan is, how its parts fit together, and where to start in the documentation.
---

Kowan is a self-hosted platform for running Docker on one server or on many. From a single web console you see what runs where and whether it is healthy, deploy containers and Docker Compose stacks, and fix problems without switching tools. It runs on your own infrastructure: no cloud account, no external database.

## How Kowan is put together

Kowan has three parts.

The **Kowan server**
:   One container with the web console, the REST API and the MCP server for AI assistants. It also manages the Docker host it runs on, with no extra setup.

**Agents**
:   A small container on every other server you want to manage. An agent connects out to the Kowan server, so servers behind NAT or a firewall need no open ports, or the server connects to it directly when it can reach it. See [Adding servers](adding-servers.md).

**`kowanctl`**
:   The command-line client, for scripts and terminals. See [Command line](cli.md).

## What you can do with it

- **See your whole fleet.** Every container, Compose stack and deployment on every server, grouped into [applications](applications.md), with the ones that need attention first.
- **Deploy.** Containers and Compose stacks from an editor, a template or a Git repository, with every change saved as a [revision](revisions-and-rollback.md) you can roll back.
- **Keep things healthy.** [Image updates](image-updates.md) with automatic rollback, [vulnerability scanning](vulnerability-scanning.md), [volume backups](volume-backups.md) and [disk cleanup](disk-cleanup.md).
- **Monitor.** [Alerts](alerts-and-notifications.md), [uptime checks](uptime.md), [searchable logs](logs.md) and [resource history](resource-history.md) with memory-leak warnings.
- **Work as a team.** [Roles and permissions](users-and-roles.md), [single sign-on](single-sign-on.md) and an [audit log](audit-log.md) of every change.

## Where to start

1. [Install Kowan](installation.md) on a server with Docker. It takes one command.
2. Follow [First steps](first-steps.md) to create your account and find your way around.
3. [Add your other servers](adding-servers.md).

!!! tip "Coming from Portainer?"
    Kowan can import your Portainer stacks with their environment variables. See [Importing stacks](importing.md).
