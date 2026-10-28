# Deploying a registry

**Registries → Deploy a registry** runs an image registry (`registry:3`) on one
of your hosts, publishes it on a domain with a Let's Encrypt certificate and
adds it to the registry list. Deployments, update checks and the registry
explorer can use it right away.

Code: `kowan/hostedregistry/` (service, bcrypt), `kowan/api/hostedregistry.py`,
`web/frontend/src/components/registries/DeployRegistryDialog.tsx`.

## The certificate

The registry is published the way the Exposure page publishes an app (see
[exposure.md](exposure.md)): it speaks plain HTTP on the proxy network, and
the host's Traefik routes the domain to it with a Let's Encrypt certificate it
obtains and renews by itself. Every Docker trusts Let's Encrypt, so nothing is
installed on any machine.

What it needs:

- a domain whose DNS points at the host;
- ports 80 and 443 of the host open to the internet (the HTTP-01 challenge);
- Traefik running on the host. With none, the dialog offers to deploy it (the
  same form as on the Exposure page) and the deploy is refused until it runs.

A registry reachable only on a LAN, with no domain, is not supported: no
public authority issues certificates for private addresses.

## Login

**Require a login** (on by default) adds an htpasswd user: `kowan` with a
generated password unless you give one (8 characters at least). The password
is shown once in the dialog and kept, encrypted, with the registry entry, so
Kowan's own pulls log in by themselves.

`registry:3` accepts only bcrypt in its htpasswd file. Kowan computes it in
pure Python (`kowan/hostedregistry/bcrypt.py`, cost 8, about a second) instead
of adding a dependency; the tests check it against Apache's `htpasswd`.

Without a login, anyone who can reach the registry can push and pull.

## What gets created

- A compose deployment named after the registry (`registry` by default), with
  `REGISTRY_STORAGE_DELETE_ENABLED=true`, a named volume for the images and the
  Traefik labels for the domain. No port is published on the host.
- With a login, a volume `kowan-registry-<name>-config` holding the htpasswd
  file, filled before the deploy: the hash is not in the compose file.
- A registry entry (generic, `https://<domain>`), with the user and password
  when there is a login. It turns *Verified* once the registry answers.

Deleting the deployment leaves the registry entry and the volumes; remove them
by hand if you no longer need the images.

## API

`POST /api/v1/hosts/{hostID}/registry` (admin):

```json
{ "name": "registry", "domain": "registry.example.com",
  "auth": true, "username": "kowan", "password": "" }
```

`proxyId` picks the Traefik when the host runs more than one. The answer
carries the URL, the address to use in image references, the Traefik used and
the generated password (once).
