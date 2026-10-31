---
title: Crash snapshots
description: When a container dies with an error, Kowan saves its last logs, configuration and environment at that moment, so you can understand the crash after it was restarted or replaced.
---

When a container dies with an error and nobody stopped it, Kowan saves a **snapshot** of it at once, before a restart policy or a redeploy replaces it. You find them in the **Crashes** tab of the workload, with the number of crashes on the tab.

## What a snapshot holds

- How it ended: the exit code in words (killed for using too much memory, segmentation fault, SIGKILL…), Docker's error, when it started and ended, its restarts and restart policy.
- The last 200 log lines.
- The image, command, working folder and user, memory and CPU limits, mounts, networks and the last health check.
- The environment variables. Values of names that look secret (`PASSWORD`, `TOKEN`, `SECRET`, `KEY`, `DATABASE_URL`…) are hidden.
- The workload's CPU and memory in the half hour before.

## What counts as a crash

A container that ends with a non-zero exit code, without a `docker stop` or `docker kill` just before. Stopping a container from Kowan, the CLI or an orchestrator is not a crash; a process killed by the kernel for memory is.

A container that keeps crashing gets one snapshot every 5 minutes; the crashes in between are counted on it (**+3 more**). Kowan keeps the last 20 snapshots per container, for 30 days.

To leave a container out, label it `kowan.snapshot: "off"`.

## With alerts and assistants

An alert rule on crashes (`container_exited`, `container_restart_loop`) can also [run a runbook](runbooks.md#when-an-alert-fires), for example to collect more diagnostics. Through the [MCP server](mcp.md), `crash_snapshots` lets an assistant read the snapshots to explain a crash.

## Who can see them

Whoever can read the workload. Deleting one needs operate.
