---
title: First steps
description: Create the first Kowan administrator with the one-time setup token, learn your way around the web console, and deploy your first workload.
---

Once the Kowan server is running, open `http://<your-server>:5081` in a browser. The first visit asks you to create the administrator account; after that, the server Kowan runs on is ready to manage.

## Create the administrator

A fresh server has no users. So that whoever reaches it first cannot take it over, the first-run screen asks for a one-time **setup token** that only someone with access to the server can read. The server prints it in its log:

```bash
docker logs kowan
```

With Docker Compose, run `docker compose logs server`. Look for the line that starts with `first-run setup: open the web UI and enter this setup token`. The token is also saved as the file `setup-token` in the data volume.

1. Open `http://<your-server>:5081`. Kowan shows **Create the administrator**.
2. Paste the token into **Setup token**.
3. Choose a **Username** (`admin` by default) and a **Password** of at least 8 characters, and repeat it in **Confirm password**.
4. Click **Create administrator**. Kowan signs you in and opens the **Overview**.

Once the first account exists, the setup screen is gone for good and the token file is deleted. Everyone else signs in from the **Sign in** page with an account you create for them. See [Users and roles](users-and-roles.md).

### Create the administrator without the browser

For automated installs, set these environment variables on the server container:

| Variable | Purpose |
|---|---|
| `KOWAN_ADMIN_PASSWORD` | Creates the first administrator with this password at startup, with no setup screen. |
| `KOWAN_ADMIN_USER` | Username of that administrator. Defaults to `admin`. |
| `KOWAN_SETUP_TOKEN` | Keeps the setup screen but uses this token instead of a generated one. A token you set is not printed in the log. |

Kowan reads `KOWAN_ADMIN_PASSWORD` only while no user exists, so it never overwrites a real account. Change the password after the first sign-in; you can then remove the variable. See [Configuration](configuration.md) for every setting.

## Find your way around

The console has three fixed parts: a sidebar on the left, a top bar, and the page itself.

The host picker
:   The block at the top of the sidebar sets which server every page shows. Pick **All hosts** to see your whole fleet, or one host to focus on it. **Manage hosts…** and **Add a host** are at the bottom of its list.

The sidebar
:   Pages grouped by purpose. **Infrastructure** holds the Overview, Workloads, Templates, Images, Volumes, Networks and the Network map. **Automation** holds Auto-update, Backups, Schedules, Exposure, Workflows and GitOps. **Activity** holds the Timeline, Alerts, Uptime, Logs and the Audit log.

Settings
:   At the bottom of the sidebar. It opens its own area: your profile, appearance, server updates and backups under **General**; registries, credentials, secrets and notification channels; users, teams, API keys, single sign-on and AI assistants; and the server's **Hosts & agents** and **Network access**. Some entries are visible to admins only.

The search palette
:   Press ⌘K on a Mac or Ctrl+K elsewhere, or click the search box in the top bar, to jump to any workload, container, host, page or action. Press Tab to narrow the results to one kind.

The top bar
:   The bell holds notifications, such as alerts and new Kowan releases. The account menu on the right shows who you are signed in as, the theme picker and **Log out**.

The Docker host Kowan runs on is already listed, under the name **local**. You can rename it from its page or from **Settings → Hosts & agents**.

## Deploy your first workload

Everything that runs on your servers shows up on the **Workloads** page, including containers and Compose stacks started outside Kowan. To start something new:

1. Go to **Workloads** and click **New workload**.
2. Pick where it comes from:
    - **From a template**: a ready app, such as a database or a monitoring tool, with its settings filled in.
    - **Compose or container deployment**: paste a Compose file or describe a single image.
    - **From a git repository**: the same form, with Git as the Compose source.
    - **Import stacks**: bring in stacks from Portainer or a folder of Compose files. See [Importing stacks](importing.md).
3. Fill in the form and save. Kowan checks the Compose file while you edit it and again before it deploys.

The new workload opens on its own page, with its containers, logs, configuration and history. See [Applications](applications.md) for how workloads are organized and [Deployments](deployments.md) for every deployment option.

## Make it yours

A few settings worth changing early:

- **Change your password** in **Settings → General → Profile**.
- **Pick a theme** from the account menu or **Settings → General → Appearance**. **Auto** follows your system's light or dark mode.
- **Add your team** in **Settings → Users**, with the admin, operator or viewer role. See [Users and roles](users-and-roles.md).
- **Get alerts** by email or chat: add a channel in **Settings → Notifications**. See [Alerts and notifications](alerts-and-notifications.md).
- **Turn off what you do not use.** **Settings → General → Features** removes Templates, the Network map or GitOps from the navigation for everyone.

## Next steps

- [Adding servers](adding-servers.md): manage more than one Docker host.
- [Backup and restore](backup-and-restore.md): protect the server's data before you rely on it.
- [Command line](cli.md): use `kowanctl` from a terminal.
