# Connections

A **connection** is a typed credential that can be reused across services: one
AWS account, one GitHub token, one Vault server. Services do not copy its secret;
they point at it by id and ask it for a **capability**, a verb Kowan can perform
with it.

## Three nouns

| Noun | What it is | Where it lives |
|---|---|---|
| Connection | Reusable secret material plus its type. Encrypted `data`, plain `meta`. | A `credentials` row with `kind = connection` |
| Capability | Something a connection can do: `dockerRegistryAuth`, `secretFetch`, `messageSend`, `gitFetch`, `httpBearer` | Code: `kowan/capabilities.py` |
| Service config | A registry, secret or notify channel that needs a capability. It holds its own non-secret settings (region, path, channel) and `meta.connectionRef` | A `credentials` row of `kind` `registry`, `secret` or `channel` |

The capability is asked for by the caller, not fixed by the type: the same `aws`
connection can authenticate ECR pulls and fetch from Secrets Manager.

## Code

- `kowan/capabilities.py`: the manifest of connection type to capabilities.
  Cloud types derive their facets from `cloudIdentity`.
- `kowan/connections.py`: `build_capability`, `resolve`, `resolve_service`. A
  service with a `connectionRef` uses the connection's credentials; one without
  uses its own inline auth.
- `kowan/connection_types.py`: field schemas of the native types, `git-http`,
  `github`, `gitlab`, `bitbucket` (gitFetch + httpBearer), `api-token` and
  `basic-auth` (httpBearer).

`objectStore`, `sshAccess`, `tlsClientAuth` and `gitApi` are names with no
builder yet: `build_capability` raises `CapabilityError` for them, and no type
claims one.

## Who uses it

- **Registries**: pull auth and validation (`kowan/registry/resolve.py`).
- **Notify**: channels and the workflow notify step send through `messageSend`.
- **External secrets** (`vault`, `infisical`, `phase`): a secret can take its
  backend (address, token) from a connection and keep its own mount, path and
  key, so many secret references share one Vault.
- **Git-commit trigger**: through `[[secret]]` interpolation; a secret that
  delegates to a git connection authenticates the clone.

## What stays out

Interpolation secrets (`[[name]]`, a named encrypted value) are not connections:
they implement no capability. Neither are single-use values such as a webhook
URL that is itself the secret, workflow run tokens, API keys, or infrastructure
keys (`KOWAN_SECRET_KEY`, `KOWAN_JWT_SECRET`, TLS certificates).

Kowan does not merge credentials that happen to hold the same secret into one
connection: the same password is not the same identity.
