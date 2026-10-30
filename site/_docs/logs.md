---
title: Logs
description: Stream live container logs from every host, keep a workload's logs after restarts and redeploys, and search them by words, phrases and time range.
---

Kowan streams live logs from any container on any host, and can keep a workload's logs in its own searchable archive. Docker holds only a container's recent output and loses it when the container is replaced; retained logs survive restarts, redeploys and replaced containers.

## Watch live logs

1. Go to **Activity → Logs** and stay on the **Live** tab.
2. Pick one or more containers in the list on the left. Filter it by name or image, and turn off **Running only** to include stopped containers.

The output of every container you pick merges into one stream, each line tagged with its container (and its host when you look at all hosts). The same viewer is on a workload's **Logs** tab and on a container's page.

The viewer's controls:

**Filter**
:   Terms separated by spaces must all match: `error`, `"connection lost"` for a phrase, `-debug` to exclude, `/user \d+/i` for a regular expression, `level:error,warn` for levels.

Level buttons
:   **All**, **Err**, **Warn**, **Info** or **Dbg**.

**Tail**
:   How many recent lines to start from: 100, 200, 500 or 1000.

**Follow**
:   Streams new lines as they come. Turn it off to load a snapshot instead, refreshed every few seconds with **Auto-refresh**.

**Timestamps** and **Wrap**
:   Show Docker's timestamps, and wrap long lines.

You can also download the lines on screen, or clear the view.

To follow a container's log from a terminal, use the [command line](cli.md):

```bash
kowanctl container logs <container-id> --host <host-id>
```

Without `--host`, it reads from the server's own host.

## Keep a workload's logs

1. Go to **Infrastructure → Workloads**, open the workload and go to its **Logs** tab.
2. Under **Keep logs**, choose how long to keep them: 1, 7, 14, 30 or 90 days.

A workload is a deployment, a Compose stack or a lone container; see [Applications](applications.md). From then on, Kowan follows the logs of the workload's running containers on every host. It starts with the last 200 lines each container still holds, then stores every new line. It looks for new containers every 5 seconds, so a redeploy, a scale-up or a restart is picked up on its own.

Turning retention on, changing it and stopping it need the operator or admin role.

## Search retained logs

1. Go to **Activity → Logs** and open the **History** tab, or click **Search history** next to **Keep logs** on a workload.
2. To narrow the search, pick workloads in **Workloads keeping logs**. With none picked, Kowan searches them all.
3. Type your search, choose a time range (from **Last hour** to **Last 30 days**, or **Everything kept**) and click **Search**.

Results come newest first, 200 lines at a time. Click **Load older lines** for more.

| You type | Kowan finds lines that |
|---|---|
| `timeout upstream` | contain both words |
| `"connection refused"` | contain the exact phrase |
| `error -healthcheck` | contain "error" but not "healthcheck" |

Search matches whole words: `time` does not find "timeout". Other operators, such as `OR` or `*`, are searched as plain text. Users limited to some resources by a team only find lines from the containers they can read.

## Change or stop retention

In the **History** tab, open the menu next to a workload in **Workloads keeping logs**:

- **Keep for** sets a new retention period.
- **Open workload** goes to the workload's **Logs** tab.
- **Stop keeping…** stops collecting and deletes every stored line of that workload.

You can also choose **Off** under **Keep logs** on the workload. Live logs are not affected either way.

## Storage and limits

- Retained logs live in a separate database, `logs.db`, in Kowan's data volume. Kowan's own [backup](backup-and-restore.md) does not include it.
- Once an hour, Kowan deletes lines older than their workload's retention, and the lines of workloads that no longer keep logs.
- The archive holds at most 2 million lines in all. Above that, the oldest lines go first, whatever their retention.
- Lines longer than 8192 characters are cut.
- Kowan follows only running containers. When it reconnects to one, after a host comes back online or the server restarts, it reads the last 1000 lines again and stores the ones it missed.

## Use the API

```text
GET    /api/v1/log-archive/retention              workloads that keep logs, with line counts
PUT    /api/v1/log-archive/retention/{workload}   start keeping or change: {"days": 30} (operator)
DELETE /api/v1/log-archive/retention/{workload}   stop keeping and delete the stored lines (operator)
GET    /api/v1/log-archive/search                 search the kept lines
```

A workload id has the form `dep~<deployment-id>`, `stack~<host-id>~<project>` or `ctr~<host-id>~<container-name>`; you find it at the end of the workload's page address. Through the API, `days` goes from 1 to 365.

The search takes `q` (the same syntax as above), `workload` (repeatable), `host`, `hours` or `since` and `until` (RFC 3339 times), and `limit` (1 to 1000, default 200). It returns the lines newest first, and a `next` value to pass as `before` for the following page:

```bash
curl -H "X-API-Key: $KOWAN_API_KEY" \
  "https://kowan.example.com/api/v1/log-archive/search?q=%22connection+refused%22&hours=24"
```

An AI assistant connected through the [MCP server](mcp.md) can search the retained logs too. See [REST API](api.md) and [API keys](api-keys.md).
