# Compose validation

Kowan checks a compose file before it is saved or deployed, and live while you
edit it in the deployment form (YAML and Easy mode).

## Endpoint

`POST /api/v1/compose/validate` (operator)

```json
{"compose": "services: ...", "hostId": "local", "deploymentId": "…", "env": {"TAG": "1.2"}}
```

`hostId` enables the host checks (ports, container names, bind paths);
`deploymentId` excludes that deployment's own containers so a redeploy does not
conflict with itself; `env` lists variables known to be set (e.g. a `.env`).

Response (always 200 for a bad file: problems are diagnostics):

```json
{
  "diagnostics": [
    {"severity": "error", "code": "port-in-use",
     "message": "host port 8080/tcp is already used on the target host by 'web-1'",
     "path": "services.web.ports[0]", "line": 7, "column": 9,
     "fix": {"description": "Publish on 8081 instead",
             "patch": [{"op": "set", "path": ["services", "web", "ports", 0], "value": "8081:80"}]}}
  ],
  "engine": "docker",
  "checks": {"hostPorts": true, "hostPaths": false, "secrets": true},
  "summary": {"error": 1, "warning": 0, "info": 0}
}
```

* `line` / `column` are 1-based, `0` when unknown.
* `fix` is present only when the change is safe. `patch` operates on the YAML
  *document* (`set` creates/replaces a node, `remove` deletes a key or item),
  so the UI applies it with a comment-preserving YAML library in either editor
  mode. `kowan/compose/patch.py` is the server-side twin.

## Structural validation

1. `docker compose -f - config --quiet`, run in an empty temporary project
   directory with a minimal environment (the server image ships the compose
   plugin). Error messages are mapped back to positions (`L3.C5`, `line 4`,
   `services.web.ports`, `service "web"`).
2. When the CLI is unavailable, a built-in schema-lite check: top-level keys,
   service shape, list/map types, undeclared networks, duplicate keys
   (`code: schema`).

YAML syntax errors are reported as `yaml-syntax` with the parser's position.
Kowan's `[[secret]]` references are masked with same-length filler before
parsing, so they never break the YAML and positions stay exact.
`env_file` is read on the target host, so a missing one is `env-file-unchecked`
(info), not an error; a relative one also gets the `relative-path` warning.

## Lint rules

| code | severity | fix | meaning |
|---|---|---|---|
| `image-no-tag` | warning | | image has no tag (implicitly `:latest`) |
| `image-latest` | warning | | image pinned to `:latest` |
| `restart-missing` | info | `restart: unless-stopped` | no restart policy (`deploy.restart_policy` counts) |
| `privileged` | warning | | `privileged: true` |
| `network-host` | warning | | `network_mode: host` |
| `pid-host` | warning | | `pid: host` |
| `docker-socket` | warning | | the Docker socket is mounted |
| `port-in-use` | error | next free port | published port used by a running container on the host |
| `port-duplicate` | error | | two services publish the same host port |
| `bind-path-missing` | warning / error | | absolute bind source missing on the host (short syntax: Docker creates an empty root-owned dir; long syntax fails) |
| `relative-path` | warning | | relative bind source, `env_file` or config/secret `file:`. Every compose deployment (inline or from git) reaches the host as text written to a temporary file, so docker compose resolves these in that temporary folder, not next to the original file |
| `volume-undeclared` | error | declare it | named volume not under top-level `volumes:` |
| `healthcheck-missing` | info | | a service others depend on has no healthcheck |
| `healthcheck-required` | warning | | `condition: service_healthy` on a service without healthcheck |
| `depends-on-unknown` | error | remove the entry | `depends_on` names an unknown service |
| `container-name-dup` | error | remove it | two services share a `container_name` |
| `container-name-in-use` | error | remove it | `container_name` already taken on the host |
| `env-unset` | warning | | `${VAR}` without default and not in `env` |
| `secret-missing` | error | | `[[name]]` matches no Kowan secret |
| `secret-field-missing` | error | | `[[name.field]]`: no such field |
| `secret-field-ambiguous` | error | | bare `[[name]]` on a multi-field secret without `value` |

Host checks run only when the host answers within `KOWAN_COMPOSE_HOST_TIMEOUT`
(5s). Bind paths are checkable only for the local host when the server itself
is not containerised; otherwise the rule is skipped.

## Where it runs

* **UI**: `ComposeBuilder` validates 600 ms after the last edit and lists the
  diagnostics under the editor; YAML mode shows inline lint marks
  (`@codemirror/lint`, already part of the editor's basic setup). A position
  jumps to the line (switching Easy mode to YAML), "Apply fix" applies the
  patch. Saving is blocked on errors; warnings need an explicit
  "Deploy with N warnings" confirmation, scoped to that warning set.
* **Server**: create, update and deploy of an inline-compose deployment
  re-validate; any `error` returns `400 {error, diagnostics, ...}`. Warnings
  never block. Git-sourced compose is validated by GitOps plans, not by this
  gate. `KOWAN_COMPOSE_VALIDATE=0` disables the gate.

Code: `kowan/compose/` (`positions.py`, `structural.py`, `lint.py`,
`validate.py`, `patch.py`), `kowan/api/compose_validate.py`.
