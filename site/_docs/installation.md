---
title: Installation
description: Install Kowan on a Linux server with Docker using docker run or Docker Compose, then serve it securely over HTTPS.
---

Kowan runs as a single container on any Linux server with Docker. Installing it takes one command, and the server it runs on is ready to manage as soon as you sign in.

## Requirements

- A Linux server with Docker Engine, on x86-64 (amd64) or ARM64.
- Port **5081** reachable from your browser, or a reverse proxy in front of it.

To manage more servers later, each one needs Docker and the Kowan agent. See [Adding servers](adding-servers.md).

## Install with docker run

```bash
docker run -d --name kowan --restart unless-stopped \
  -p 5081:5081 \
  -v kowan-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/kowan-lab/kowan:latest
```

Then open `http://<your-server>:5081`. The first screen asks for a one-time setup token, which the server prints in its log (`docker logs kowan`). Continue with [First steps](first-steps.md).

## Install with Docker Compose

Save this as `docker-compose.yml`:

```yaml
services:
  server:
    image: ghcr.io/kowan-lab/kowan:latest
    ports:
      - "5081:5081"
    volumes:
      - kowan-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped

volumes:
  kowan-data:
```

Then start it:

```bash
docker compose up -d
```

## What the two volumes do

`kowan-data` mounted at `/data`
:   Everything Kowan keeps: its database, the keys that sign sessions and encrypt your secrets, and your Compose stacks. It is the only thing you need to back up. See [Backup and restore](backup-and-restore.md).

The Docker socket, `/var/run/docker.sock`
:   Lets Kowan manage the Docker host it runs on, and update itself from **Settings**. Without it, manage this server through an agent like any other.

!!! warning "The Docker socket is root access"
    Whoever controls Docker on a server controls the server. Keep Kowan on a trusted network or behind HTTPS with strong passwords, and restrict who can reach it with [network access rules](network-access.md).

## Set the external URL

If you will add other servers or use single sign-on, tell Kowan the address people and agents use to reach it:

```bash
-e KOWAN_EXTERNAL_URL=https://kowan.example.com
```

Kowan uses it in the commands it gives you for new agents and in sign-in redirects. Every other setting is optional; see [Configuration](configuration.md).

## Serve Kowan over HTTPS

By default Kowan serves plain HTTP on port 5081. Before you reach it over the internet, serve it over HTTPS in one of two ways:

- **Behind a reverse proxy** such as Traefik, Caddy or nginx that terminates HTTPS and forwards requests to port 5081. WebSocket connections must pass through, since live logs, the terminal and edge agents use them.
- **With its own certificate**: mount a certificate and its key into the container and point `KOWAN_TLS_CERT` and `KOWAN_TLS_KEY` at them. See [Configuration](configuration.md).

When the proxy adds the client's address in a header, list the proxy in `KOWAN_TRUSTED_PROXIES` so that Kowan records the right address in the audit log and applies [network access rules](network-access.md) to the real client.

## Next steps

- [First steps](first-steps.md): create the admin account and find your way around.
- [Adding servers](adding-servers.md): manage more than one Docker host.
