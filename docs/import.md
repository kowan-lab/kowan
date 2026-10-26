# Importing stacks

Bring stacks that already run elsewhere under Kowan: from Portainer, or from
a folder of compose stacks. Each becomes a deployment, created and not
deployed; one imported under its compose project name takes over the
containers that already run (Kowan finds them by the project label).

Code: `kowan/importer/` (`portainer.py`, `compose_dir.py`),
`kowan/api/imports.py`; UI: `pages/Import.tsx` (Workloads → New workload →
Import stacks).

## Portainer (admin)

`POST /api/v1/import/portainer/scan {url, apiKey | username+password, verifyTls}`
reads `/api/endpoints`, `/api/stacks` and each stack's file through
Portainer's API. The token or password is used for this read and not kept.
Compose stacks come back with their variables applied; git stacks come back
as git sources (a private repository needs a credential after importing, and
their Portainer variables belong in the repository's `.env`); swarm stacks
keep the swarm strategy; Kubernetes stacks are listed and not importable. The
suggested host is the one named like the Portainer environment.

## Compose folders (operator)

The browser reads the picked folder (only `compose.yaml`, `compose.yml`,
`docker-compose.y(a)ml` and `.env`, 1 MiB each) and posts the files;
`POST /api/v1/import/compose/scan {files}` groups them one stack per folder
and applies the `.env`.

## Apply

`POST /api/v1/import/apply {items: [{name, host, strategy, compose | git}]}`
creates each through the same checks as `POST /deployments` (name, host,
compose gate, permissions) and reports per item. Revisions record source
`import`.
