# Kowan — Changelog

## 0.1.3 — 2026-10-26

- **Settings at the bottom of the sidebar**, instead of in the account menu; it opens the same Settings area.
- **Kowan's own containers hidden** from Workloads, the dashboard, Logs and search: the server, the agents and their helpers (updates, scans, volume browsing, backups). Settings → Features shows them again (`systemContainers`, off by default).
- **Old Swarm tasks are no longer crashes.** Swarm stops the previous task on every update (exit 143, or 137 past the grace period) and keeps it as history; when a newer task runs in the same slot it now shows as "Replaced" instead of a critical card in Needs you or a degraded workload.
- **Auto-heal sees a compose service that is gone.** A compose target was judged by its first container, so with nginx up and backend and frontend removed it read "running" and nothing redeployed. Every declared service now counts (a one-shot that exited 0 is done): the target turns `degraded`, naming what is missing or stopped, and the reconcile loop redeploys it. The loop now runs every 5 s (`KOWAN_RECONCILE_INTERVAL`); the 120 s cooldown per deployment stays.
- **A missing service shows.** The workload turns red ("backend, frontend are missing") and Needs you gets a card for it.
- **Up out of requested.** A workload's "x/y" is replicas up out of those requested: each service's replicas from the compose file, or one per Swarm slot, instead of every container ever kept. A Swarm service outside any stack is now one workload with its tasks, labelled Swarm.
- **Switching host opens its overview**, from the host menu and from search.
- **Workload services fold.** A workload's page groups its containers by service, each row with its state and count ("1 + 4 replaced"), and remembers which are open; the Workloads table shows the same list. Old Swarm tasks fade under the live one.
- **Scale a service in place.** Each service of a deployment has a Replicas control: Apply writes `deploy.replicas` into the compose file (comments kept) as a new revision and redeploys. A service with `container_name` or a fixed host port stays at one; a git compose file is changed in its repository.
- **Ports in a fixed order.** Docker lists a container's ports in a different order on every call; they are now sorted (container port, protocol, host port, IPv4 before IPv6). The container page also sorts its mounts, networks and aliases.
- **Images: updates and bulk remove.** An Update column says which images have a newer digest; each image can be checked against its registry on the spot (`GET /hosts/{id}/images/{image}/update`, also for images no container runs) and the newer one pulled. Filters (in use, unused, untagged, update available) and multi-select remove many images at once.
- **No leftover auto-update policies.** Saving a policy equal to the defaults (mode off, nothing else set) deletes it instead of storing it, so cycling the Updates knob back to off leaves nothing behind; an old row like that no longer shows in Auto-update.

## 0.1.2 — 2026-10-26

- **New release notices**: every 6 hours the server checks the published versions and, when one is newer than its own, posts "Kowan X.Y.Z is available" to the bell, once per version. `KOWAN_RELEASE_REPOSITORY` sets where it looks (empty turns it off).
- **Settings → Updates** shows the running version and the latest release (it showed a registry digest next to the version), also for a server run from source, where self-update is not available. **Check** now asks the registry again: it used to repeat the answer cached for 5 minutes. Agents show the same.
- **Auto theme**: next to Light and Dark, Auto follows the system and switches with it; it is the default for anyone who never chose.
- **Environment variables up front** when creating or editing a container deployment: their own section under the ports, instead of the third tab of the advanced settings, where they were easy to miss.
- **Faster backend tests**: `make test` and CI run one test module per CPU (`scripts/run_tests.py`, standard library only): about 26 s instead of 2.5 min here, coverage unchanged. `make test-serial` keeps the single-process run.
- Fixed: right after the host booted, the reconcile loop did not restart deployments whose containers were down (for as long as its cooldown, 2 minutes by default): a deployment never reconciled counted as reconciled at boot time.
- Fixed: an auto-update's **stability window of 0 seconds** (Settings) was read as unset and waited 10 seconds after every update.
- **Features on/off** in Settings (admins): Templates, Network map and GitOps can be turned off for everyone. They leave the navigation, the command palette and the links to them; their API, GitOps webhooks included, answers 404 "turned off in Settings", and GitOps stops syncing. Nothing is deleted; `GET/PUT /api/v1/features`.
- **Stack variables** for compose deployments, like a Portainer stack's environment: name/value pairs that fill the compose's `${VAR}`s when it deploys (for a git source, over the repository's `.env`). They are part of the revision history, masked for viewers, copied to preview environments and not to promotion targets (which take the artifact, never the environment); GitOps leaves them in Kowan. The live compose check counts them as set.
- **Config or secret, per environment row**, for stack variables and a container deployment's environment alike: a Secret row's value goes to an encrypted secret the deployment owns (`<deployment>-env`) and the row keeps a reference, so the value is never stored in the deployment or sent back; operators can set it, admins can reveal it. The secret is deleted with the deployment, shows its workload on the Secrets page and cannot be renamed. Shared secrets from the Secrets page are referenced as before, `[[name.field]]`.
- **Import, reworked**: a Portainer stack's environment and a folder's `.env` become stack variables, each proposed as config or secret, and the compose stays as written. "Write into the compose" (0.1.1) is gone: a variable that should be plain is simply not a secret.
- **Portainer import through Credentials**: a new Portainer connection type keeps the address and the access token (or username and password) encrypted; the import page picks one, or creates it on the spot, instead of asking every time.
- **Container actions everywhere**: Kill (SIGKILL, or another signal through `POST …/containers/{id}/kill?signal=`), on the container page, the agent and MCP; and a "⋯" menu on every container of a workload, in its page and in the Workloads table, with start, stop, restart, pause, unpause, kill and remove.

## 0.1.1 — 2026-10-26

- **Rename hosts**, the local one included: from the host's page, the Hosts list (admins) or `kowanctl hosts rename <id-or-name> <new-name>`; API `PATCH /api/v1/hosts/{id}`. Only the displayed name changes: the id, deployments, permissions and history stay the same. A name may not repeat another host's name or id (GitOps files use either), and the same check now applies when a host is created.
- **Copy buttons over HTTP**: opening Kowan as `http://<address>` (neither HTTPS nor localhost), the browser offers no Clipboard API, and copy buttons did nothing (a host's join command, API keys, secrets, URLs). They now fall back to the classic copy and, if that fails too, say so.
- **Portainer import, variables as encrypted env**: all of a stack's variables go to an encrypted Kowan secret `<stack>-env` (one field per variable) and the compose references them as `[[<stack>-env.VAR]]`. In the import preview you pick, variable by variable, which ones to write into the compose instead; those used in an `image:` line are flagged. After the import, a deployment's Config tab (admins) lists the compose's `[[secret.FIELD]]` references and writes the chosen ones into the compose ("Write into the compose"), as a new revision and without a redeploy.
- **Two-level stacks** in Workloads: unmanaged containers group into a stack when they come from a swarm stack (`com.docker.stack.namespace`) too, not only from a compose project; opening a stack lists its services, and each service shows its containers (its replicas, for swarm) with a summary state. Retained logs, resource history and disk cleanup use the same rule.
- CI: `coverage` 7.16.2 on Python 3.10 (7.17 needs 3.11), and the end-to-end test with Docker matches the Overview tab.
- Documentation, the changelog and the Makefile are in English.
- The frontend builds with **Node 26** (LTS from October 28; 24 moved to maintenance): the image's build stage, CI (`actions/setup-node` v7) and `@types/node` 26. Dependabot leaves `@types/node` majors alone: they follow the build's Node.

## 0.1.0 — 2026-10-26

First public release. Images `ghcr.io/kowan-lab/kowan` and `ghcr.io/kowan-lab/kowan-agent` (amd64 and arm64), the CLI with `pip install git+https://github.com/kowan-lab/kowan`. MIT license.

**What it contains**
- **Server, agent and CLI**: a Python control plane with SQLite and no external database; agents connected by an authenticated tunnel, in edge mode (the agent calls the server, behind NAT too) or direct mode (the server calls the agent); `kowanctl` for the terminal.
- **Docker resources** on every host: containers, images, volumes, networks and stacks, with live logs, a web console and a file browser.
- **Deployments** from an editor, a template or a git repository, with compose validation, revisions with diff and rollback, previews for branches and pull requests, promotion from staging to production, and import from Portainer or compose folders.
- **Safe updates**: vulnerability scans with Trivy, auto-update with maintenance windows and automatic rollback when the health check fails; server and agent updates with rollback.
- **Exposure and HTTPS** through Traefik, a network map, volume backups with restic (local disk, S3, SFTP), scheduled start/stop, GitOps, workflows and webhooks.
- **Observability**: uptime with an optional public status page, retained and searchable logs, resource history with a memory-growth warning, alerts by email and chat, a daily or weekly digest, guided disk cleanup.
- **Access**: admin/operator/viewer roles, teams with per-resource permissions, OpenID Connect SSO, API keys, an audit log, network allowlists, an MCP server for AI assistants.
- **A React interface** with six light and dark themes, usable on a phone and installable as an app.

The sections below tell the development that led to 0.1.0.

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
