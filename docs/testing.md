# Tests

Kowan has three levels of tests. Each runs locally with one command, and in CI
(`.github/workflows/ci.yml`) on every push and pull request.

| Level | What it covers | Command | Where |
|---|---|---|---|
| Backend | REST API, store, auth, mux/tunnel, agent, Docker (a fake Engine), CLI, workflows, GitOps, notifications | `make test` / `make coverage` | `tests/` |
| Frontend | every page and component of the SPA in jsdom, against a fake backend | `make test-ui` / `make coverage-ui` | `web/frontend/src/**/__tests__/` |
| End to end | the built SPA against a real server, in a browser | `make e2e` | `web/frontend/e2e/` |

`make test-all` runs all three. `make deps-dev` installs `coverage` and
Playwright's browser.

## Backend

The standard library's `unittest`, no plugins. The base pieces:

- `tests/apicase.py::ServerCase` mounts the whole app (middleware, modules, a
  SQLite store in a temporary folder) and offers `call(method, path, role=…)`
  and `roles()` to try every endpoint with every role.
- `tests/fakeengine.py::FakeEngine` is an in-memory Docker Engine served on a
  unix socket: containers, images, volumes, networks, exec, logs, stats,
  events, pulls with digests (`registry`), injected errors (`fail`,
  `fail_once`) and containers that exit with a code (`runners`).
- Registries, secret servers (Vault, Infisical, Phase), SMTP and HTTP receivers
  are fakes too: `kowan.httpcap.mock(handler)` (or `httpcap.Client(handler=…)`)
  answers outbound requests in place of the network, and the CLI's
  `kowan.cli.httpclient.Session(handler=…)` does the same for `kowanctl`.
- The agent runs for real: `test_agent_e2e.py` connects it to the server in
  edge and direct mode, and `test_remote_client.py` repeats the local client's
  suite through the tunnel.

`make test` and `make coverage` run `scripts/run_tests.py`: one test module
per process, on every CPU (`JOBS=n` limits it), with the standard library
only. The modules are independent (temporary folders, random ports); the
slowest start first, by the durations of the previous run kept in
`.test-timings.json`. With coverage each process writes its own data file and
they are combined at the end. A failing module's output is printed in full;
`make test-serial` runs everything in one process, for a test that only fails
next to others.

`make coverage` writes `coverage.json` and `htmlcov/` and fails below the
`fail_under` threshold in `.coveragerc` (lines + branches). Only
`raise NotImplementedError` stubs, the `if __name__ == "__main__":` guard and
`if TYPE_CHECKING:` imports are excluded.

## Frontend

Vitest + Testing Library in jsdom. The tests mount the real app (router, auth,
host, react-query, theme) with `renderApp(path, { role, api })` and use it the
way a person would: clicks, typing, selects, dialogs, toasts.

- `src/test/api.ts` replaces `fetch`, `WebSocket` and `EventSource`. Routes are
  `"METHOD /path"` relative to `/api/v1` (`:param`, `*`); a handler is the body
  to return or a function of the request, and `reply(status, body, headers)`
  covers errors and headers. Every request lands in `calls`;
  `requests(method, pattern)` filters them. `socket(part)` returns the
  WebSocket the page opened, to send it messages.
- `src/test/render.tsx::baseApi` gives every test a plausible backend
  (`fixtures.ts`, plus the server's real catalogs in `catalog.json`: credential
  types, volume plugins, triggers, alert rules, templates).
- `src/test/ui.ts` has helpers for the kit: `dialog`, `choose` (Select),
  `press`, `toast`.
- After each test, the setup waits for in-flight queries and mutations to
  settle (a late toast must not leak into the next test) and fails if the same
  request went out more than 150 times: the sign of a refetch loop.

`make coverage-ui` fails below the thresholds in `vitest.config.ts`.
`npm run typecheck:test` checks the types of the tests and the e2e specs
(`tsconfig.test.json`; the app build leaves them out).

## End to end

Playwright (`web/frontend/playwright.config.ts`) starts `kowan.server` on
`127.0.0.1:18777` with a fresh data folder (`e2e/.data`, wiped at every start)
and a fixed setup token, and opens the SPA built into `web/dist` (`make e2e`
builds it first).

- `setup.e2e.ts` goes through the first run in the UI (a wrong token, then the
  right one), creates the admin and saves the session for the other specs.
- `smoke.e2e.ts` opens every page and fails on JavaScript or console errors.
- `flows.e2e.ts`: a viewer created by the admin sees the console read-only, an
  encrypted secret is revealed and resolves masked, a workflow runs a script and
  shows its output, an alert rule is created, disabled and deleted, and the
  local host is renamed and named back.
- One spec per feature: `history` (revisions and rollback), `uptime` (a check
  on the server itself and the public status page), `logs` (retention and
  search), `previews` (previews of a `.invalid` repository, so no network),
  `promotion` (staging → production without deploying), `import` (a compose
  folder read by the browser), `digest` (schedule and preview, without the
  updates section that would query registries).
- `clipboard.e2e.ts` opens the server as `http://kowan.internal:18777`, which is
  not a secure context, and copies the join command without the Clipboard API.
- `mobile.e2e.ts`: at 390 × 844 no page scrolls sideways, tables become cards,
  the menu is a drawer; the manifest, icons and service worker, and the
  installed console starting offline.
- `docker.e2e.ts` (only with `KOWAN_E2E_DOCKER=1`, on in CI) deploys an `alpine`
  container named `kowan-e2e-*`, checks the resources panel and "Keep logs",
  stops it and removes it.

The server uses the local Docker: the specs only read from it, except
`docker.e2e.ts`, which touches only the container it creates. No spec prunes:
the disk cleanup page is opened, never run, and no spec turns on scheduled
cleanup.

## Writing a new test

- An endpoint: a method in a `ServerCase`, with `roles()` if it depends on the
  role.
- A page: `renderApp("/path", { api: { "GET /…": data } })`, then interactions
  with `user` and assertions on `requests(...)` and on the visible text. Routes
  passed in `api` win over the defaults.
- A bug: first the test that reproduces it, then the fix.
