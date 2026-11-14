---
title: Configuration
description: Every environment variable the Kowan server and agents read, with defaults: keys, network allowlists, retention, SSO and tuning.
---

Kowan works without configuration. Everything you can change is an environment variable: set it on the server container to configure the control plane, and on an agent container to configure that host. A variable you leave unset keeps the default shown here.

The external URL is not a variable: an admin sets it from **Settings → Server address**. `KOWAN_EXTERNAL_URL` only fills it in on the first start, when nothing is saved yet.

## Set a variable

With `docker run`, pass `-e` once per variable:

```bash
docker run -d --name kowan --restart unless-stopped \
  -p 5081:5081 \
  -v kowan-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e KOWAN_TRUSTED_PROXIES=172.18.0.0/16 \
  ghcr.io/kowan-lab/kowan:latest
```

With Docker Compose, use `environment`:

```yaml
services:
  server:
    image: ghcr.io/kowan-lab/kowan:latest
    environment:
      KOWAN_TRUSTED_PROXIES: 172.18.0.0/16
```

Docker sets a container's environment when it creates the container. To change a variable, recreate the container: `docker compose up -d` does it for you.

### Value formats

Durations
:   A number with a unit: `s`, `m`, `h` or `d`, combined if you like (`15s`, `168h`, `90d`, `1h30m`). A whole number counts as seconds.

Seconds
:   Variables marked "seconds" take a plain number, such as `120`.

Address lists
:   Comma-separated IP addresses or CIDR ranges, IPv4 or IPv6: `10.0.0.0/8, 192.168.1.10`. Empty means no limit.

Switches
:   `off`, `false`, `no` or `0` turn a switch off. Anything else, or leaving it unset, keeps it on.

## Server variables

### General

| Variable | Default | Description |
|---|---|---|
| `KOWAN_ADDR` | `:5081` | Address and port the server listens on, as `host:port`. `:5081` listens on every interface. |
| `KOWAN_DATA_DIR` | `/data` | Where Kowan keeps its database, keys and Compose stacks. The image sets `/data`; run from source, the default is `./data`. |
| `KOWAN_TLS_CERT` | none | Path to a PEM certificate. With `KOWAN_TLS_KEY` also set, the server serves HTTPS itself instead of plain HTTP. |
| `KOWAN_TLS_KEY` | none | Path to the PEM private key of that certificate. |
| `KOWAN_DOCKER_SOCKET` | `/var/run/docker.sock` | Path to the socket of the Docker Engine on the server's own host. Set it for rootless Docker or Podman, which put their socket elsewhere. |
| `KOWAN_SERVER_CONTAINER` | none | Name or ID of the Kowan server container. Set it only if the server update in **Settings** cannot find its own container: by default Kowan looks for its hostname, then for a container named `kowan-server`. |

### First admin and keys

| Variable | Default | Description |
|---|---|---|
| `KOWAN_ADMIN_PASSWORD` | none | Creates the first admin account with this password when the server starts with no users, instead of the setup screen. Ignored once any account exists. |
| `KOWAN_ADMIN_USER` | `admin` | Username of that first admin. It is also the default break-glass account in SSO-only mode. |
| `KOWAN_SETUP_TOKEN` | generated | One-time token the setup screen asks for before it creates the first admin. When unset, Kowan generates one, prints it in the server log and saves it as `setup-token` in the data directory. It is deleted once the first admin exists. |
| `KOWAN_SECRET_KEY` | generated | Master key that encrypts stored secrets and credentials. When unset, Kowan generates one and keeps it as `secret.key` in the data directory. Any string works: a base64 value of exactly 32 bytes is used as is, anything else is turned into a key by hashing. |
| `KOWAN_JWT_SECRET` | generated | Key that signs sign-in tokens. When unset, Kowan generates one and keeps it as `jwt.key` in the data directory. Changing it signs everyone out; API keys keep working. |

!!! warning "Keep your keys with your backups"
    Without its key, Kowan cannot decrypt stored secrets. A generated key lives in the data volume and is part of every [backup](backup-and-restore.md). If you set `KOWAN_SECRET_KEY` or `KOWAN_JWT_SECRET` yourself, there is no file to back up: keep the values safe, and set the same ones on any server you restore to.

### Network and security

| Variable | Default | Description |
|---|---|---|
| `KOWAN_UI_ALLOW_CIDRS` | no limit | Addresses allowed to reach the web console, the REST API and the MCP endpoint. Webhooks (`/hooks/...`) and `/health` stay open, since CI systems and Git hosts call them. |
| `KOWAN_EDGE_ALLOW_CIDRS` | no limit | Addresses allowed to connect edge agents to the server. |
| `KOWAN_TRUSTED_PROXIES` | none | Reverse proxies allowed to state the client's address. Kowan ignores the client-address header from anyone else, so a client cannot pose as another address in the audit log or the allowlists. |
| `KOWAN_PROXY_HEADER` | `X-Forwarded-For` | The header your trusted proxies use: `X-Forwarded-For` or `X-Real-IP`. Kowan reads only that one. |
| `KOWAN_SERVER_SCRIPTS` | on | `off` disables workflow script steps, which run bash on the server. |

Admins can narrow the two allowlists further from **Settings > Network access**, but never widen them. See [Network access](network-access.md).

### Single sign-on

| Variable | Default | Description |
|---|---|---|
| `KOWAN_SSO_PROVIDERS` | none | A JSON array of sign-in providers that Kowan creates or updates at startup, matched by `name`. |

Single sign-on also needs the external URL, set in **Settings → Server address**. To keep the client secret out of the JSON, put it in another variable and name that variable in `clientSecretEnv`:

```bash
KOWAN_SSO_PROVIDERS='[{"name":"kc","type":"oidc","label":"Keycloak",
  "issuer":"https://kc.example.com/realms/main","clientId":"kowan",
  "clientSecretEnv":"KC_SECRET","scopes":"openid profile email",
  "config":{"defaultRole":"viewer",
            "roleMappings":[{"claim":"groups","value":"kowan-admins","role":"admin"}]}}]'
KC_SECRET=your-client-secret
```

See [Single sign-on](single-sign-on.md) for the provider fields and role mappings.

### Performance

| Variable | Default | Description |
|---|---|---|
| `KOWAN_UVLOOP` | on | `off` makes the server use the standard Python event loop instead of uvloop. |

## Settings that used to be variables

These are no longer environment variables. An admin edits them in the console, and a change applies at once, with no restart. Durations are written like `30s`, `15m`, `2h` or `90d`.

- **Settings → Data retention**: resource history, audit log, resolved alerts, workflow runs and the monitoring interval. See [Audit log](audit-log.md), [Alerts and notifications](alerts-and-notifications.md) and [Resource history](resource-history.md). Retained container logs are kept per workload, for the number of days you choose there. See [Logs](logs.md).
- **AI assistants → Browser access**: the allowed web origins, one per line. See [AI assistants](mcp.md).
- **Settings → Advanced**: the reconcile interval and cooldown, the GitOps tick, the Git timeout, the Compose validation switch and timeouts, the Trivy image, cache volume and container prefix, the agent image and the release repository. See [Compose validation](compose-validation.md) and [Vulnerability scanning](vulnerability-scanning.md).
- **Settings → Single sign-on**: SSO only and the break-glass user. `KOWAN_SSO_PROVIDERS` is still a variable.

Each old variable is read only once. On a server where nothing is saved yet for that setting, the variable's value becomes the setting, so an install keeps its behavior after you upgrade. After that the variable is ignored, and the console says it can be removed.

## Agent variables

Set these on the agent container of each server you add. The join command Kowan gives you already sets the ones you need. See [Adding servers](adding-servers.md).

| Variable | Default | Description |
|---|---|---|
| `KOWAN_AGENT_MODE` | `agent` | `edge`: the agent connects out to the Kowan server, so the host needs no open port. `agent`: direct mode, the agent listens and the server connects to it. |
| `KOWAN_AGENT_TOKEN` | none | The token Kowan generated for this host. Required: in edge mode the agent sends it, in direct mode it checks that the server presents it. In direct mode the agent refuses to start with a token shorter than 32 characters. |
| `KOWAN_SERVER_URL` | none | Edge mode: the Kowan server URL the agent connects to, such as `https://kowan.example.com`. Required in edge mode. |
| `KOWAN_AGENT_ADDR` | `:9001` | Direct mode: address and port the agent listens on. |
| `KOWAN_AGENT_TLS_CERT` | none | Direct mode: path to a PEM certificate for the agent's listener. Without one, the agent generates a self-signed certificate at startup. The connection handshake is tied to the certificate the agent serves, so no certificate authority is needed. |
| `KOWAN_AGENT_TLS_KEY` | none | Direct mode: path to the PEM private key of that certificate. |
| `KOWAN_AGENT_ALLOW_CIDRS` | no limit | Direct mode: addresses allowed to connect to the agent. Set it to your Kowan server's address. |
| `KOWAN_DOCKER_SOCKET` | `/var/run/docker.sock` | Path to the socket of the Docker Engine on this host. |
| `KOWAN_UVLOOP` | on | `off` makes the agent use the standard Python event loop instead of uvloop. |

## Command-line variables

`kowanctl` reads `KOWAN_SERVER`, `KOWAN_TOKEN` and `KOWAN_API_KEY`, and its MCP bridge also reads `KOWAN_URL`. See [Command line](cli.md).
