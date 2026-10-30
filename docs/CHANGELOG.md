# Kowan — Changelog

Each release lists first what changes for you; stability fixes and internal work are under "Other changes".

## 0.1.14 — 2026-10-31

- **Agents fix their own restart policy.** An agent installed without a restart policy, as the join command did up to 0.1.12, sets `unless-stopped` on its own container when it starts, so it comes back after a reboot. Update your agents from **Settings → Server updates** and there is nothing to run by hand; a policy you chose is left as it is.

## 0.1.13 — 2026-10-31

- **What's new in the console.** **Settings → What's new** shows what changed in each release, with the version this server runs marked.
- **Documentation on kowan.io.** Installation, a guide for every feature and the reference for configuration, container labels, the CLI, the REST API and MCP, searchable, at [kowan.io/docs](https://kowan.io/docs/).
- **Hosts by their name.** A renamed host shows its name on the workload pages, in search and in the host menu, instead of its id (such as `local`).
- **Every user can change their password.** **Settings → General → Profile** now works for operators and viewers too, and asks for the current password.
- **Agents come back after a reboot.** The join command starts the agent with `--restart unless-stopped`.
- **Team permissions cover schedules, volume backups and auto-update.** A team member sees and changes only those for the hosts, deployments and stacks they can access.
- **Safer teams and workflows.** A team with API keys bound to it can no longer be deleted (its keys used to keep working with their full role). A workflow with a **Backup** step is admin-only and writes only inside the backups folder. A disabled workflow refuses its webhooks.
- **AI assistants follow the API roles.** Through MCP, removing containers, images and volumes, pruning, disk cleanup and creating or destroying deployments need the operator role, as through the REST API, and destructive tools still ask for confirmation. An assistant no longer needs an admin key, which ignores team permissions.
- **Backups say what they hold.** A Kowan backup includes the keys that decrypt your stored secrets, and **Settings → General → Backup & Restore** now says so: keep backups as safe as the server.

### Other changes

- **kowanctl reads a flag, then the environment, then the config file.** An environment variable now overrides the saved settings.
- **Compose validation warns about relative paths.** A `./` bind mount, an `env_file` or a config `file:` resolves in a temporary folder on the host, not next to the original file.
- **Compose port checks are named `port-in-use` and `port-duplicate` again.**
- **Clearer texts.** The deployment's Configuration tab says when saving redeploys at once, the History tab names every revision source, registry links point at **Settings → Registries**, and the Users page no longer mentions LDAP.
- **Reconcile settings accept durations.** `KOWAN_RECONCILE_INTERVAL` and `KOWAN_RECONCILE_COOLDOWN` take values such as `5s` or `2m`.

## 0.1.12 — 2026-10-30

- **CPU is a share of the whole host.** 100% now means every core is busy, for containers, hosts and workloads alike; it used to be 100% per core, so a 4-core host could read 400%. Remote hosts switch once their agent is updated.
- **A stopped container is no longer a crash.** A container stopped on purpose that exits with an error code (as `npm start` does) shows as Stopped, without the degraded state or the "container exited" alert.

### Other changes

- **CPU of a workload on several hosts.** The hosts' average, and the busiest host's peak.
- **Tests no longer hang on Python 3.10.** The test runner also stops a module that runs too long and shows where it hung.

## 0.1.11 — 2026-10-28

- **Update a whole workload.** "Update · N" in a workload's header updates every service with a newer image at once, verified, and rolled back service by service if one fails.
- **Newer version tags.** "New versions · N" offers the newer tags of a pinned image (`api:2.0` → `api:2.1`) and shows what will change before applying it as a new revision.
- **Deploy a registry from Kowan.** Registries → Deploy a registry runs one on a host behind Traefik with a Let's Encrypt certificate, with an optional login.
- **Pull image asks for the registry first.** Browse a stored registry's repositories and tags, or type an image and see which credentials the pull uses.

### Other changes

- **Compose deploys pull with the stored credentials.** A new tag on AWS ECR no longer needs a manual pull first.
- **Updating the agents no longer marks deployments as failed.** Kowan waits for the host to come back instead of redeploying.
- **Updating a container of a deployment updates the deployment.** The next deploy no longer undoes it.

## 0.1.10 — 2026-10-28

- **The MCP server covers the whole of Kowan.** 51 tools: an assistant sees workloads as the console does, searches logs, reads resource history, revisions and audit, rolls back, promotes and plans a disk cleanup. See `docs/mcp.md`.

## 0.1.9 — 2026-10-28

- **Search (⌘K) finds workloads.** Stacks and deployments show next to containers, hosts and pages, ranked by match and filtered by kind; empty, it opens on what needs attention.

### Other changes

- **Update checks work for images on AWS ECR.** They use the ECR credentials configured for the host.

## 0.1.8 — 2026-10-28

- **Agents update together.** Every agent updates at the same time with its own health check, and one that fails rolls back alone.
- **The update dialog shows progress.** Every host stays listed while it restarts, with a bar for its current step and one for the whole update.

## 0.1.7 — 2026-10-27

- **A new look, the same on every page.** A header card with breadcrumb, title, facts and actions; sections in cards, tabs in a toolbar.
- **Workloads in tabs.** All, Managed and Not managed.
- **A clearer workload page.** CPU, memory and network side by side; updates, backups, schedule and domains switched inline.
- **A clearer container page.** How it runs and how it is wired in one card, with live resources and its stack beside it.
- **A host's overview.** Its resources, the first workloads and what needs attention.

## 0.1.6 — 2026-10-27

### Other changes

- **Updating an agent or restarting the server no longer marks deployments as failed.** An unreachable host is left alone until it is back. A deployment already marked this way needs one Redeploy.

## 0.1.5 — 2026-10-27

- **Update to the new release in one click.** A server or agent pinned to a release (`kowan:0.1.3`) now moves to the newest one; the button says which ("Update to 0.1.4").

## 0.1.4 — 2026-10-27

- **Browsing large volumes no longer uses memory.** Browse, download and upload stream through server and agent; a huge folder shows its first 5000 entries.

### Other changes

- **Bounded memory for incoming data.** Tunnel streams, exec output, the agent protocol and every outbound HTTP call have a limit.
- **Lighter host metrics.** Long ranges come back as at most 720 points.
- **Less background work.** One container list per host per reconcile pass, and stale state dropped from memory.
- **Lighter agent and image.** httpx is gone and the agent skips server-only modules.
- **Lighter interface.** Log streams pause in hidden tabs and the CSS is 30 KiB smaller.
- **Removed unused API.** `GET /scans/summary` and the `kowan.hidden` label; volume browse answers `{entries, truncated}`.

## 0.1.3 — 2026-10-26

- **Settings in the sidebar.** At the bottom, instead of in the account menu.
- **Kowan's own containers are hidden.** Out of Workloads, the dashboard, Logs and search; Settings → Features shows them again.
- **A missing compose service shows and heals.** The workload turns red naming what is missing, Needs you gets a card, and the deployment is redeployed.
- **Workload services fold.** Each with its state and replicas ("1 + 4 replaced").
- **Scale a service in place.** A Replicas control writes `deploy.replicas` into the compose as a new revision and redeploys.
- **Images: updates and bulk remove.** An Update column, an on-the-spot check and pull, filters and multi-select.
- **Switching host opens its overview.**

### Other changes

- **Old Swarm tasks are no longer crashes.** A task replaced in its slot shows as "Replaced".
- **Replica counts.** "x/y" is replicas up out of those requested, per service or Swarm slot.
- **Faster self-healing.** The reconcile loop runs every 5 s (`KOWAN_RECONCILE_INTERVAL`); the 120 s cooldown per deployment stays.
- **Ports, mounts and networks in a fixed order.**
- **No leftover auto-update policies.** A policy set back to the defaults is removed.

## 0.1.2 — 2026-10-26

- **New release notices.** The bell says when a newer Kowan is out (checked every 6 hours; `KOWAN_RELEASE_REPOSITORY`).
- **Running and latest version in Settings.** Also when running from source; Check asks the registry again.
- **Auto theme.** Follows the system, and is the default.
- **Stack variables.** Values for a compose deployment's `${VAR}`s, like a Portainer stack's environment, with revisions and masking.
- **Config or secret, per variable.** A secret value goes to an encrypted secret the deployment owns and is never shown back.
- **Features on/off.** Templates, Network map and GitOps can be turned off for everyone in Settings.
- **Portainer import, reworked.** Environments become stack variables, and connections are kept in Credentials.
- **Container actions everywhere.** Kill, and a "⋯" menu on every container of a workload.
- **Environment variables up front.** Their own section when creating or editing a container deployment.

### Other changes

- **Deployments restart right after the host boots.** The reconcile loop no longer waits for its cooldown.
- **A stability window of 0 seconds is honoured.** It used to wait 10 seconds after every auto-update.
- **Faster backend tests.** One module per CPU: about 26 s instead of 2.5 min.

## 0.1.1 — 2026-10-26

- **Rename hosts.** The local one included, from its page, the Hosts list or `kowanctl hosts rename`.
- **Copy buttons work over plain HTTP.**
- **Two-level stacks.** In Workloads a stack lists its services, each with its containers.
- **Portainer import keeps variables encrypted.** A stack's variables go to an encrypted secret.

### Other changes

- **The frontend builds with Node 26.**
- **CI fixes.** `coverage` 7.16.2 on Python 3.10, and the end-to-end test matches the Overview tab.
- **Documentation, the changelog and the Makefile are in English.**

## 0.1.0 — 2026-10-26

First public release: images `ghcr.io/kowan-lab/kowan` and `ghcr.io/kowan-lab/kowan-agent` (amd64 and arm64), the CLI with `pip install git+https://github.com/kowan-lab/kowan`. MIT license.

- **Server, agent and CLI.** A Python control plane with SQLite; agents over an authenticated tunnel, behind NAT too; `kowanctl` for the terminal.
- **Docker resources.** Every host's containers, images, volumes and networks, with live logs, a web console and a file browser.
- **Deployments.** From an editor, a template or git, with revisions, rollback, branch previews, promotion and import.
- **Safe updates.** Trivy scans, auto-update with maintenance windows and rollback, self-updates for server and agents.
- **Exposure, backups and automation.** HTTPS through Traefik, network map, volume backups with restic, schedules, GitOps, workflows and webhooks.
- **Observability.** Uptime and status page, searchable logs, resource history, alerts, digest, disk cleanup.
- **Access.** Roles, teams, SSO, API keys, audit log, network allowlists and an MCP server.
- **A React interface.** Six themes, usable on a phone and installable as an app.

## 2026-10-26 — Ten new features, phones and an installable app

**Features** (each with backend, UI and end-to-end tests; details in `docs/`)
1. **Deployment history**: every save that changes what runs is a numbered revision, with its author and reason (edit, git push, GitOps, rollback, promotion, import…). A YAML diff between any two revisions and one-click rollback; rolling back a git deployment pins it to that revision's commit. The workload's History tab, `kowanctl deployment history | diff | rollback`. → `docs/deployment-history.md`
2. **Uptime**: HTTP(S) and TCP checks with expected codes, a keyword, response times, incidents, alerts on channels, certificate expiry; they can be linked to a workload. An optional public status page at `/status` (no addresses or errors). → `docs/uptime.md`
3. **Retained, searchable logs**: a workload "keeps logs" for N days; the server follows them on every host and indexes them (SQLite FTS5, a separate `logs.db`). Logs → History searches words, phrases and exclusions across hosts and time ranges, with paging; teams see only their own containers. → `docs/retained-logs.md`
4. **Resource history** per workload (CPU, memory and network every 5 minutes, for 30 days) on the overview, and a **memory-growth warning**: a steady climb without restarts becomes an alert with the rate and, when there is a limit, the hours left. → `docs/resource-history.md`
5. **Preview environments** for branches and pull/merge requests from git deployments: created and updated by the deployment's webhook (GitHub, Gitea, GitLab) or by hand, removed when the PR closes, the branch is deleted or after N hours without a push. Fixed ports and names are taken out of the compose so they run next to the parent; `KOWAN_PREVIEW_*` variables for domains. `kowanctl deployment previews | preview`. → `docs/previews-and-promotion.md`
6. **Promotion** staging → production: copies the artifact (the image, the compose services' images edited in place, or the exact git commit), never the environment; a preview of the changes with a diff before applying them, as a new revision of the target. `kowanctl deployment promote [--dry-run]`.
7. **Import** from Portainer (a token or a login, used only to read) and from folders of compose stacks picked in the browser. Stacks become deployments, created and not started; with the same project name they adopt the containers already running. → `docs/import.md`
8. **Guided disk cleanup** with an estimate per category and careful defaults (a deployment's stopped containers, named volumes and recent images stay out), and **scheduled cleanup**, daily or weekly, with the safe categories only. → `docs/disk-cleanup.md`
9. **Periodic digest**: one message a day or a week with pending updates, images with critical or high CVEs, failed backups, expiring certificates and open alerts; a preview of the text and an immediate send. → `docs/digest.md`
10. **Phones and PWA**: every page usable on a phone (tables become cards, side-by-side panels stack), the console installable as an app with icons, shortcuts and an offline start that says so; the service worker keeps only the interface, never data. → `docs/mobile-pwa.md`

**Tests**
- Backend: 1165 tests, 90.0% coverage (lines + branches), the `.coveragerc` threshold raised to 89. New tests also for error paths of the deployment service, the agent protocol, auto-update, self-update, GitOps and backups.
- Frontend: 465 tests, 87% of lines; the `vitest.config.ts` thresholds raised.
- End to end: 51 Playwright scenarios (plus the optional Docker one), including history, uptime, logs, previews, promotion, import, digest, the phone layout and the offline PWA.

**Fixes**
- CLI: `kowanctl` dropped the git source when creating or editing a deployment: a git deployment edited from the command line became an inline one.
- Overview: "Reclaim" deleted, in one click, every stopped container (including those of a deployment stopped on purpose) and every unused volume; it now opens the guided cleanup.
- App start: without a network, or with the server restarting, the session was ended and you landed on the login page; now only a 401/403 ends it.
- Backup restore: an archive with a symbolic link under `stacks/` failed with an unreadable error instead of skipping the link.
- Deployment creation checks port conflicts leaving out the containers of the same compose project (needed to adopt existing stacks).

## 2026-10-26 — Tests for everything, and the bugs they found

**Tests**
- Backend: 1033 tests, 90% line coverage (88% lines + branches). New tests for the edge and direct agent end to end, the Docker client through the tunnel, the `kowanctl` CLI, workflows and the scheduler, GitOps, volume backups, external secrets (Vault, Infisical, Phase), notifications (a fake SMTP), SSO, scans, team permissions, store and daemon errors.
- Frontend: from zero to 377 tests of pages and components (Vitest + Testing Library), 86% of lines. Every page is exercised as a user would against the fake backend: creating, editing, errors, permissions per role.
- End to end: Playwright against a real server (first run, every page without errors, users and roles, secrets, workflows, alerts, deploying a real container).
- `make coverage`, `make test-ui`, `make coverage-ui`, `make e2e`, `make test-all`; minimum coverage thresholds in `.coveragerc` and `vitest.config.ts`; CI runs everything. See `docs/testing.md`.

**Fixes, backend**
- Reading a volume's file through the tunnel returned empty content.
- The agent's 4xx errors arrive as 400/401/403/404/409 instead of a generic 502.
- Resolving external secrets (Vault, Infisical, Phase) blocked the server's asyncio loop during deploys, workflows, GitOps and previews.
- A CIFS volume (`//server/share`) was taken for a host path.
- Creating a user with a name already in use gave 500 instead of 409.
- Exposing a container or a deployment again lost the label of the network Kowan had attached it to.
- Stats and exec of a missing container gave 502 instead of 404.
- Restoring a backup extracts into a temporary folder and replaces the data folder only after checking it.
- `KOWAN_ADDR` with only a host (no port) was not accepted.
- The GitOps webhook checks "sync disabled" before the path exclusions.
- Invalid JSON bodies on networks, plugins and volumes gave 500 instead of 400.
- CLI: compatibility with typer 0.27 (which bundles click).

**Fixes, interface**
- A rendering error on one page blanked the whole app: now the page shows the error and a "Try again", and the shell stays.
- When `GET /hosts` failed, pages in their error state went into a request loop (thousands a second) and the host detail stayed on a spinner.
- Secrets from a connection: the form asked again, as required, for the fields the connection already has (the Vault address, for example).
- SSO providers: clicking into the client secret while editing and saving erased the stored secret.
- Commands with quoted arguments (`nginx -g 'daemon off;'`): the container form split them on spaces, and the compose editor broke them whenever another field changed.
- Number fields: clearing a field that has a default put the default back and the typed digit was appended (6 → "612" hours in the auto-update interval).
- The cron field crashed the page when the preview returned no dates.
- Hosts: the agent's "Update" button in the row opened the host detail instead of the confirmation.
- Workloads: viewers saw "New workload" and the prune actions.
- New deployment: the host chosen in the shell was not preselected.
- A deployment's overview now shows health, git source and webhook (the components existed but were not wired in).
- An accessible label on the port protocol in the container form.

## 2026-10-25 — Free software, a new UI, performance

**License and distribution**
- **MIT** license. Builds and images use public packages only.
- The server's default port is **5081** instead of 8080 (in goroawase, 5-0-8-1 reads *ko-o-wa-n*). `KOWAN_ADDR` changes it as before.
- Docker images on multi-stage Alpine with a final `FROM scratch`: the server from 138 to 59 MB compressed, the agent from 102 to 57 MB. A HEALTHCHECK and the `org.opencontainers.image.version` label. The docker/compose binaries are verified by checksum at build time.

**Security** (the October 25 audit, P0 items)
- First-run setup protected by a one-time token (in the server log or `setup-token` in the data volume; `KOWAN_SETUP_TOKEN` for provisioning).
- Sessions checked against the database on every request: logout revokes the token, a password change ends the other sessions, a deleted user is blocked at once, and the role is read from the database, not the token.
- Workflow "script" steps: a minimal environment (never the server's), no export of `PATH`/`BASH_ENV`/`LD_*`, admins only, and they can be turned off with `KOWAN_SERVER_SCRIPTS=off`.
- HTTP steps: webhook payload values go in last and literally, and can no longer resolve `[[secrets]]`.
- Viewers no longer receive webhook tokens, git push secrets, workflow run output and environment variable values (containers, specs, compose).
- Tunnel: the token is checked before the WebSocket upgrade, the handshake has a timeout, messages are capped at 64 KB; the agent does not start without a token of at least 32 characters.
- Network allowlists `KOWAN_UI_ALLOW_CIDRS` and `KOWAN_EDGE_ALLOW_CIDRS` on the server, `KOWAN_AGENT_ALLOW_CIDRS` on the agent; the client address comes from a single header (`KOWAN_PROXY_HEADER`) and only from the `KOWAN_TRUSTED_PROXIES`.
- Limits on login attempts (per address and per user) and on concurrent hashing; passwords of at least 8 characters everywhere.
- Security headers: a strict CSP, `frame-ancestors 'none'`, nosniff, `Referrer-Policy`, HSTS over HTTPS.

**Security, second pass** (a review of the fixes and the dependencies)
- Python dependencies pinned to exact versions checked against OSV (`requirements.txt`); in `pyproject.toml`, floors without known advisories (aiohttp ≥ 3.14.4, cryptography ≥ 49, orjson ≥ 3.11.6). `websockets`, never used, removed.
- react-router-dom 6.30.6; redirects after login only to local paths.
- The script-step rule is shared between REST and MCP (through MCP an operator could still run them).
- The compose view for those who cannot operate is built from an allowlist (no anchors, `x-` fields, build args, inline secrets, commands); through MCP too. Credentials removed from git URLs, workflow extra arguments and HTTP headers masked. The decision uses per-resource grants, not only the global role.
- YAML: a limit of 100,000 nodes after alias expansion (a "billion laughs" bomb refused in 11 ms).
- Builds from git like deployments: URLs and refs validated, `--`, limited protocols, a context that cannot leave the temporary folder. Docker/git commands with timeouts and bounded output.
- Outgoing HTTP responses read up to a limit (RSS, git, HTTP steps, notifications, registry tokens).
- Webhook tokens masked in logs.
- `X-Forwarded-For` with ports and unreadable values handled without falling back to the proxy's address; a single header (`KOWAN_PROXY_HEADER`). The server's allowlist for edge agents is now called `KOWAN_EDGE_ALLOW_CIDRS`.
- A login limit that cannot lock the admin out: per address+user, per address (IPv6 per /64), per user only past 100 failures and never from addresses already used successfully.
- A role change ends sessions; a timeout on the agent's edge handshake too; fixed `is_admin()`, which gave 500 to users with scopes.

**Security, direct mode (stoa)**
- The token no longer travels: server and agent prove to each other that they know it with an HMAC challenge-response bound to the fingerprint of the agent's TLS certificate. An interceptor with its own certificate is refused by both sides, and the server sends nothing to a fake agent. No configuration needed, even with the certificate regenerated at every start.
- Before the upgrade the agent accepts only a time-bound HMAC of the token (±2 minutes), not the token.

**Settings > Network access**
- The allowlists for the UI and edge agents can be managed from the UI too (admins only): they narrow the environment's, never widen them; saving asks for the password and is refused if it would exclude the address you are connecting from. Trusted proxies and the header stay in the environment, shown read-only.
- Changing your own password no longer ends the session of the tab in use (the server returns a new token); the other sessions end.

**Creating deployments**
- A new "After creating" section (and "More options" in the template dialog): deploy now, auto-update with a window, volume backups, start/stop times, a domain through the reverse proxy, a redeploy webhook and deletion protection are chosen at creation and applied as soon as the deployment is saved. If something fails, the deployment stays created and a notice says what to finish.
- The images include `tzdata`: without it, schedules in a time zone other than UTC failed (the images start `FROM scratch` and have no `/usr/share/zoneinfo`).

**Exposure**
- caddy-docker-proxy support removed: Kowan detects, installs and manages Traefik only (detection, labels, certificates, choice of network).

**Dependency updates**
- Frontend: React 19.3, react-router 8 (from 6: no open advisories, `npm audit` at zero), Vite 8 with the Rolldown bundler (builds from ~6 s to ~0.6 s) and plugin-react 6, TypeScript 7 (the native compiler, typecheck in ~1 s), xterm 6, react-query, codemirror and yaml at their latest. `@types/node` stays on 22, like the build's Node.
- The first page downloads 224 KB compressed instead of 388: the YAML editor (CodeMirror) ended up in the initial load because of how chunks were grouped; it now loads only where needed.
- Images: Python 3.14.9 (3.15 has no musl wheels for pyyaml and frozenlist yet) and Node 24.22.1 LTS, pinned by digest; docker CLI 29.9.2 and compose 5.6.1. Verified with a real compose deploy and a real edge agent from the new image.
- A new "Page not found" page for unknown addresses (the page used to stay blank).

**Interface**
- Moved to **shadcn/ui + Tailwind v4** with an internal kit (`src/ui/kit`). A technical-tool visual direction: no radii, flat buttons, one font (Geist Mono), the full viewport, bands instead of cards, compact density. See `DESIGN.md`.
- **6 themes** (Kowan, Nord, Dracula, Catppuccin, Gruvbox, Tokyo Night), light and dark, generated by `scripts/gen-themes.py`.
- A new shell: the host picker at the top, a ⌘K command palette, navigation by purpose; the shell stays mounted between pages.
- A dashboard with a fleet table, containers to watch (exit codes), open alerts and light SVG charts.
- New pages: **Templates** (22 ready apps), **Activity** (live Docker events), **AI assistants**.

**Features**
- **Image updates** for containers: a badge, a check, pull and recreate.
- **Deployments from Git**: repository, branch, compose path, signed webhooks (GitHub/Gitea/GitLab), "Pull & redeploy".
- **A template catalog** with inputs and generated secrets.
- **A Docker event stream** across all hosts (REST + WebSocket).
- **Retention** of the audit log (90 days), resolved alerts (30 days) and workflow runs (the last 100), configurable.
- **Server and agent updates with automatic rollback** (`kowan/updater`, `docs/updates-design.md`): pull, an architecture check, a smoke test of the new image on a copy of the database, a SQLite snapshot, a transactional swap run by a helper started from the current image, a health check (healthcheck, stable restarts) and automatic restore of containers and database on failure. The previous version is kept for manual rollback, with a full history, and agents update in a rolling fashion, stopping at the first failure. Verified on real Docker in every failure scenario.
- Server and agent shut down at once even with tunnels and WebSockets open (it used to take 20 s and a SIGKILL on every update).
- **A built-in MCP server** (`POST /mcp`, Streamable HTTP, no new dependencies): 29 tools filtered by role, explicit confirmation for destructive actions, an audit entry for every action, resources and prompts. A `kowanctl mcp` stdio bridge for Claude Desktop. See `docs/mcp.md` and the "AI assistants" page.

**Performance**
- First load from ~2.3 MB to ~650 KB transferred; gzip/brotli precompressed assets and an immutable cache; compressed JSON responses.
- axios and ECharts removed; the YAML editor and the terminal load only when needed; preloading on hover; virtualized tables and logs.
- List updates **pushed** from Docker events (polling reduced to a safety net).
- A parallel monitor and one-shot stats (a cycle from ~2 s to ~70 ms), a cached image list, chunked logs, orjson + uvloop, SQLite indexes and `synchronous=NORMAL`.

**Security and correctness**
- Fixed path traversal towards the Docker Engine and mounting of the host filesystem through volume browsing; scrypt login off the event loop and in constant time; a race in the first setup.
- WebSocket heartbeats on tunnels and streams; the agent closes its streams when the client disconnects.
- Typed errors (404, 400, 409) cross the API and the agents' tunnel instead of all becoming 502; detail pages stop polling removed objects.
- About forty UI and backend bugs.
