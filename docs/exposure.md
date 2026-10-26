# Exposure and HTTPS

The **Exposure** page answers "what can the internet reach on my hosts?" and
lets an operator publish a container or compose service on a domain with
automatic HTTPS, through a reverse proxy that already runs on the host.

Code: `kowan/exposure/` (pure parsing and orchestration), `kowan/api/exposure.py`
(routes), `web/frontend/src/pages/Exposure.tsx` and
`web/frontend/src/components/exposure/`.

## What is shown

**Published ports**, per host, from the container list:

| Bind address | Reach |
| --- | --- |
| `0.0.0.0`, `::`, empty | **public**: every interface of the host |
| `127.0.0.1`, `::1` | **local only** |
| any other address | **one interface** (LAN, VPN…) |

Databases, caches, brokers, the Docker API and admin UIs (PostgreSQL, MySQL,
Redis, MongoDB, Elasticsearch, RabbitMQ management, Portainer, Adminer,
pgAdmin…) are recognised by container port, or by image name when the port is
unusual. A *public* one gets a warning. The list lives in
`kowan/exposure/classify.py`.

**Domains** routed by a proxy, parsed from container labels:

- Traefik v2/v3: `traefik.http.routers.<r>.rule=Host(`a`, `b`)` (and
  `HostSNI` for TCP routers), plus entrypoints, `tls`, `tls.certresolver`,
  the service port and `traefik.enable`. Traefik v1 `traefik.frontend.rule=Host:a,b`
  is read too.

**Proxy**: a running `traefik` image (Traefik is the only supported proxy).
Traefik's static configuration is read from its flags, `TRAEFIK_*` variables
or `/etc/traefik/traefik.yml` (read through the container archive API, so no
shell is needed): entrypoints, certificate resolvers, `providers.docker.network`.

## Certificates

"Check certificates" reads certificate status from the proxy's own storage,
never from the network:

- Traefik: each resolver's `acme.json` (path from `acme.storage`). Only names,
  issuer and validity are returned; private keys are never sent to the browser.
  When the Traefik API is enabled, router status (`enabled` / errors) is read
  with `wget` inside the Traefik container.

When it is not readable, the page shows the configured resolver only.

## Expose / Unexpose

`POST /api/v1/hosts/{hostID}/exposure/expose` (operator):

```json
{ "target": { "kind": "container", "id": "web" },
  "domain": "app.example.com", "port": 8080,
  "entrypoint": "websecure", "tls": true, "certResolver": "letsencrypt" }
```

- Labels for Traefik: `traefik.enable`, a router with `Host(`domain`)`, the
  entrypoint, `tls` + `tls.certresolver`, a service with the container port and
  `traefik.docker.network`.
- The target joins the proxy's network: `providers.docker.network` if set,
  else a network both already share, else the
  proxy's first user-defined network.
- `kowan.exposure.*` marker labels record the domain, the router, the network
  that was added and the exact keys that were set, so **Unexpose** removes only
  those and leaves hand-written labels alone.

How the change is applied:

| Target | How |
| --- | --- |
| Plain container | Recreated with a raw clone of its config (`Ops.container_relabel`): the original is renamed aside and stopped, the clone is created with the new labels and networks (aliases and static IPs kept), started, and the original removed. Any failure removes the clone and restores the original. |
| Container of a Kowan deployment (container or compose) | The deployment is edited instead (a recreate would be undone by the next deploy). |
| Container deployment | Labels and network added to the spec, then a background redeploy. |
| Compose deployment | Labels added to the service, the proxy network declared `external` under the `kowan_proxy` key, then a redeploy. The YAML is re-serialised, so comments are lost. |

Not supported: git-sourced compose files (add the labels in the repository),
swarm stacks (labels belong under `deploy.labels`).

## Deploying a proxy

When no proxy runs on a host, `POST /hosts/{hostID}/exposure/proxy`
(`{"kind":"traefik","email":"…","httpPort":80,"httpsPort":443,"staging":false}`)
creates and deploys a compose deployment with Traefik v3:

- entrypoints `web` (:80, redirects to HTTPS) and `websecure` (:443);
- resolver `letsencrypt` with the HTTP-01 challenge on `web`, storage in a
  named volume; `staging` switches to the Let's Encrypt staging CA;
- docker provider with `exposedbydefault=false` on the network `kowan-proxy`;
- the API bound to `127.0.0.1:8080` inside the container (used only to read
  router status).

Ports 80 and 443 must be reachable from the internet and the domain's DNS must
point at the host before Let's Encrypt can issue a certificate.
