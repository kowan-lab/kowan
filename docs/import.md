# Importing stacks

Bring stacks that already run elsewhere under Kowan: from Portainer, or from
a folder of compose stacks. Each becomes a deployment, created and not
deployed; one imported under its compose project name takes over the
containers that already run (Kowan finds them by the project label).

Code: `kowan/importer/` (`portainer.py`, `compose_dir.py`),
`kowan/api/imports.py`; UI: `pages/Import.tsx` (Workloads → New workload →
Import stacks).

## Portainer (admin)

`POST /api/v1/import/portainer/scan {connectionId}` reads `/api/endpoints`,
`/api/stacks` and each stack's file through Portainer's API. The address and
the access token (or username and password) come from a saved **Portainer
connection** in Credentials, stored encrypted like any credential: the import
page picks one, or creates it on the spot, so later imports need nothing
typed again. The connection's `insecure` field set to `true` accepts a
self-signed certificate.

Compose stacks come back as written, with their `${VAR}`s, and the stack's
environment becomes the deployment's **stack variables**. The preview lists
them by name (never their values) with a Secret switch, proposed on for
names that look sensitive (PASS, PWD, SECRET, TOKEN, KEY, CREDENTIAL, AUTH,
SALT, PRIVATE, DSN, CONNECTION_STRING) and for URLs that carry a user and
password. A secret variable is kept encrypted in the deployment's own secret,
exactly like one marked Secret in the deployment form (see "Environment:
config and secrets" below), so importing needs no admin.

Git stacks come back as git sources (a private repository needs a credential
after importing, and their Portainer variables belong in the repository's
`.env`); swarm stacks keep the swarm strategy; Kubernetes stacks are listed
and not importable. The suggested host is the one named like the Portainer
environment.

## Compose folders (operator)

The browser reads the picked folder (only `compose.yaml`, `compose.yml`,
`docker-compose.y(a)ml` and `.env`, 1 MiB each) and posts the files;
`POST /api/v1/import/compose/scan {files}` groups them one stack per folder;
a folder's `.env` becomes the stack variables, proposed as config or secret
the same way.

## Environment: config and secrets

A workload's environment rows (a compose deployment's stack variables, a
container deployment's environment) are each **config** or **secret**. A
config row is stored as written, and may reference a shared secret from the
Secrets page as `[[name.field]]`. A secret row's value goes to one encrypted
secret the deployment owns, `<deployment>-env`, a field per row; the row keeps
only `[[<deployment>-env.KEY]]`, so the value is never in the deployment, its
revisions or the API, and a saved secret shows as unchanged in the form until
a new value is typed. Anyone who may edit the workload can set these values;
only an admin can reveal them, on the Secrets page, where the secret names the
workload it belongs to and cannot be renamed. It is deleted with the
deployment. Changing a secret value redeploys a running workload; it is not
part of the revision history, so a rollback keeps the current value.
(`kowan/deployment/envsecrets.py`)

## Apply

`POST /api/v1/import/apply {items: [{name, host, strategy, compose | git}]}`
creates each through the same checks as `POST /deployments` (name, host,
compose gate, permissions) and reports per item. Revisions record source
`import`.
