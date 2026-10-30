---
title: Exposure and HTTPS
description: See which ports and domains your servers expose, spot public databases, and put services on a domain with automatic HTTPS through Traefik and Let's Encrypt.
---

The **Exposure** page shows what on your servers can be reached from outside: every published port by the address it is bound to, and every domain a reverse proxy routes. From there, or from any workload, you can put a service on a domain with a Let's Encrypt certificate through Traefik.

## See what is exposed

Open **Automation → Exposure**. The badges at the top count public ports, sensitive services that are public, and domains. Below them, each host lists its **Published ports** and its **Domains**.

### Published ports

The **Reach** of a port depends on the address it is bound to:

| Bind address | Reach |
| --- | --- |
| `0.0.0.0`, `::` or none | **Public**: every interface of the host |
| `127.0.0.1` or `::1` | **Local only** |
| Any other address | **One interface**, often a LAN or VPN address |

Kowan recognizes databases, caches, message brokers, the Docker API and admin interfaces (PostgreSQL, MySQL, Redis, MongoDB, Elasticsearch, RabbitMQ management, Portainer, Adminer, pgAdmin and others) by their container port, or by their image name when the port is unusual. When one of them is public, its row carries a warning.

!!! warning "Keep databases off public ports"
    A sensitive service on a public port is reachable by anyone who can reach the host. Bind it to `127.0.0.1` (`"127.0.0.1:5432:5432"` in Compose), or remove the published port and reach it from other containers over a Docker network.

### Domains

Kowan reads domains from the Traefik labels of your containers: `Host(...)` rules of HTTP routers, `HostSNI(...)` rules of TCP routers, and Traefik v1 `traefik.frontend.rule=Host:...` labels. For each domain you see:

| Column | Shows |
| --- | --- |
| **Domain** | The domain, the proxy, the router and its entrypoints |
| **HTTPS** | **http only**, the certificate resolver, or the certificate status once checked |
| **Router** | Whether the router is enabled, and **no proxy** when Traefik is not running |
| **Routes to** | The container behind the domain |
| **Port** | The container port, or **auto** |
| **Set by** | **Kowan** when Kowan added it, **labels** when the labels were written by hand |

## Requirements

- Traefik running on the host. It is the only reverse proxy Kowan detects and manages. Kowan reads its configuration from its command-line flags, its `TRAEFIK_*` environment variables or `/etc/traefik/traefik.yml`.
- For Let's Encrypt certificates, ports 80 and 443 of the host reachable from the internet, and a DNS record for each domain that points at the host.

## Deploy Traefik

If a host has no reverse proxy, Kowan can deploy one for you:

1. On **Automation → Exposure**, find the host and click **Deploy Traefik**. The **Expose** dialog offers the same form when the host has no proxy.
2. Enter the **Email for Let's Encrypt**.
3. Keep **HTTP port** 80 and **HTTPS port** 443 unless they are taken. Kowan warns you when one is already published on the host.
4. Tick **Use the Let's Encrypt staging CA** only to test: staging certificates are not trusted by browsers.
5. Click **Deploy Traefik**.

Kowan creates and deploys a Compose deployment named `traefik` with Traefik v3:

- Entrypoint `web` on port 80, which redirects to HTTPS, and `websecure` on port 443.
- A certificate resolver named `letsencrypt` that uses the HTTP-01 challenge, with certificates kept in a named volume.
- The Docker provider on the network `kowan-proxy`, routing only containers that opt in.
- Traefik's API bound to `127.0.0.1` inside the container, used by Kowan to read router status.

It appears with your other [deployments](deployments.md), so you can update or roll it back like any of them.

## Put a service on a domain

1. Create a DNS `A` or `AAAA` record for the domain that points at the host.
2. Open the expose dialog from one of these places:
    - A workload: **Policies** tab, **Exposure**, set the knob to **Domain**.
    - A deployment: **Expose** in its header.
    - A container: **More → Expose on a domain…**.
3. Fill in the form:

    | Field | Value |
    | --- | --- |
    | **Domain** | For example `app.example.com` |
    | **Container port** | The port the application listens on inside the container |
    | **Service** | For a Compose deployment with several services, the one to expose |
    | **Proxy** | When the host runs more than one Traefik |
    | **Entrypoint** | Usually the HTTPS one, `websecure` on a Traefik deployed by Kowan |
    | **Certificate resolver** | `letsencrypt` on a Traefik deployed by Kowan |
    | **HTTPS (TLS)** | On to serve the domain over HTTPS |

4. Click **Expose**.

Kowan adds the Traefik labels (router, rule, entrypoint, TLS and resolver, service port) and connects the target to the proxy's network: the network set in Traefik's Docker provider, otherwise a network they already share, otherwise the proxy's first user-defined network.

!!! warning
    With **HTTPS (TLS)** on and no certificate resolver, Traefik serves its default self-signed certificate, which browsers do not trust.

### How the change is applied

A container Kowan does not manage
:   Recreated with the same configuration plus the new labels and network. If any step fails, the original container is restored.

A Kowan deployment, or a container that belongs to one
:   The deployment is edited and redeployed, so the next deploy keeps the domain. For a Compose deployment, Kowan adds the labels to the service and declares the proxy network as external. The Compose file is rewritten, so comments in it are lost.

Two kinds of workloads cannot be exposed this way. For a deployment from a Git repository, add the labels in the repository. For a Swarm stack, the labels belong under `deploy.labels` in the stack file.

## Remove a domain

Open the expose dialog again and click **Unexpose** followed by the domain. Kowan records the labels it set, so it removes only those and the network it attached, and leaves labels you wrote by hand alone.

## Check certificates

When a host has a domain with TLS, click **Check certificates** on its section of the Exposure page. Kowan reads the certificates from Traefik's own storage (its `acme.json` file), never from the network, and shows each one as:

- **valid**, with the days left,
- **expiring**, when fewer than 14 days are left,
- **expired**.

Hover over the status to see the issuer and the expiry date. Certificates from the staging CA are marked. Only names, issuer and validity are read: private keys never leave the host. When Traefik's API is enabled, the **Router** column also shows Traefik's own status for each router.

Certificates that expire soon also appear in the [digest](digest.md). To watch a public site from the outside, add an [uptime check](uptime.md).

## API

| Method and path | What it does |
| --- | --- |
| `GET /api/v1/exposure` | Exposure of every host |
| `GET /api/v1/hosts/{hostID}/exposure` | Exposure of one host |
| `GET /api/v1/hosts/{hostID}/exposure/certificates` | Certificate and router status |
| `POST /api/v1/hosts/{hostID}/exposure/expose` | Put a container or deployment on a domain (operator) |
| `POST /api/v1/hosts/{hostID}/exposure/unexpose` | Remove it (operator) |
| `POST /api/v1/hosts/{hostID}/exposure/proxy` | Deploy Traefik with Let's Encrypt (operator) |

An expose request looks like this:

```json
{
  "target": { "kind": "container", "id": "web" },
  "domain": "app.example.com",
  "port": 8080,
  "entrypoint": "websecure",
  "tls": true,
  "certResolver": "letsencrypt"
}
```

See [REST API](api.md) for authentication.
