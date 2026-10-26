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
Compose stacks come back as written, with `${VAR}` in place, plus their
variables (`env {secret, values, inImage}`). The preview lists the variables
by name (never their values): each one goes to an encrypted Kowan secret,
`<stack>-env`, by default, or, ticked, is written into the compose. The secret
gets a field per variable (it is numbered when that name is taken) and the
compose references it as `[[<stack>-env.VAR]]`, filled in at deploy time.
Storing a secret needs an admin, as elsewhere; with every variable written in
there is no secret and an operator can import. If creating the deployment
fails, the secret is removed again. Variables an `image:` line uses are
flagged: in the secret, update checks, scans and promotion cannot read which
image runs.

Later, the deployment's Config tab (admins) lists the `[[secret.FIELD]]`
references of its compose and writes the chosen ones in:
`POST /api/v1/deployments/{id}/inline-secrets {refs: ["shop-env.TAG"]}`.
What runs does not change, so nothing redeploys; the result is a new
revision, which a rollback undoes. A value that would break the YAML is
refused.

Git stacks come back as git sources (a private repository needs a credential
after importing, and their Portainer variables belong in the repository's
`.env`); swarm stacks keep the swarm strategy; Kubernetes stacks are listed
and not importable. The suggested host is the one named like the Portainer
environment.

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
