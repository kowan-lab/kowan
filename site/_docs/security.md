---
title: Security
description: How Kowan protects your Docker hosts: sign-in, roles, agent tokens, encrypted secrets, network allowlists, the audit log, and how to report a vulnerability.
---

Kowan controls Docker on every server it manages, and control of Docker is control of the server. Kowan authenticates every person and agent, encrypts the secrets it stores and records every change. The [checklist](#hardening-checklist) covers what is up to you.

## The Docker socket is root access

The Kowan server and every agent talk to Docker through its socket, `/var/run/docker.sock`. Whoever controls Docker on a server can start a container with full access to the host, so access to Kowan is, in practice, root access to the servers it manages.

- Treat admins and operators as people with root on the servers they can reach. Operators can deploy containers and Compose stacks, and [Compose validation](compose-validation.md) warns about privileged containers, host networking and a mounted Docker socket but does not block them.
- Use [teams and grants](users-and-roles.md#teams-and-grants) to keep people to the servers and stacks they work on.
- Before you expose Kowan to the internet, serve it over HTTPS, use strong passwords or single sign-on, and restrict who can reach it with [network access rules](network-access.md).

## Signing in

**Local accounts**
:   Passwords are at least 8 characters and stored as salted scrypt hashes.

**First run**
:   Creating the first admin requires a one-time setup token that the server prints in its log, so whoever reaches a fresh server first cannot claim it. Set `KOWAN_SETUP_TOKEN` to choose the token, or `KOWAN_ADMIN_USER` and `KOWAN_ADMIN_PASSWORD` to create the admin without the setup screen.

**Repeated failures**
:   Kowan counts failed sign-ins over a 10-minute window. It refuses further attempts at one username from one address after 10 failures, and every attempt from one address after 20. After 100 failures on one username across all addresses, it also refuses that username, except from addresses where it has signed in successfully before, so an attacker cannot lock the real admin out. IPv6 addresses are counted per /64.

**Sessions**
:   A session lasts 24 hours and is checked against the database on every request. Signing out ends it. Changing a user's password or role ends their sessions (when you change your own password, the tab you are using stays signed in), and a deleted user is cut off at once.

**Single sign-on**
:   OpenID Connect or GitHub, with an optional SSO-only mode that keeps one local break-glass admin. See [Single sign-on](single-sign-on.md).

**API keys**
:   Kowan stores only a SHA-256 hash of each key and shows the full key once. See [API keys](api-keys.md).

## Roles and permissions

Every user has a role: viewer, operator or admin. Teams and grants narrow a user to specific servers, deployments, stacks or labeled resources, with the role as the ceiling. Viewers see the names of environment variables but not their values. See [Users, roles and teams](users-and-roles.md).

## How agents authenticate

Each server you add gets its own token, and an agent refuses to start with a token shorter than 32 characters. One rule covers both connection modes: whoever opens the connection proves it knows the token, and the other side verifies it.

**Edge mode**
:   The agent connects out to the Kowan server and presents its token, which the server checks against that host's record. With an `https://` server URL, the connection is encrypted and the agent verifies the server's certificate.

**Direct mode**
:   The Kowan server connects to the agent over TLS. The agent uses a self-signed certificate generated at startup, or yours from `KOWAN_AGENT_TLS_CERT` and `KOWAN_AGENT_TLS_KEY`. The token never crosses the network: both sides prove they know it with a challenge-response bound to the fingerprint of the agent's certificate. A machine in the middle with its own certificate fails on both sides, and the server sends nothing to an impostor.

Anyone who has an agent's token can act as that agent or, in direct mode, drive Docker on that server: keep it as secret as a root password. See [Adding servers](adding-servers.md).

## Secrets at rest

Kowan encrypts stored secrets, registry credentials, notification channel credentials, single sign-on client secrets and volume backup repository credentials with AES-256-GCM. The API returns these values masked. An admin can reveal a stored secret on purpose, and that request is recorded in the audit log.

| What | Where it comes from |
|---|---|
| Encryption key | `secret.key` in the data volume, generated at first start, or `KOWAN_SECRET_KEY`. |
| Session signing key | `jwt.key` in the data volume, generated at first start, or `KOWAN_JWT_SECRET`. |

!!! danger "Protect the data volume and its backups"
    If you lose `secret.key`, the secrets it encrypted cannot be recovered. A [server backup](backup-and-restore.md) includes both keys when they are kept as files, so anyone with the backup can decrypt your secrets: store it as carefully as the server. To keep the keys out of backups, set `KOWAN_SECRET_KEY` and `KOWAN_JWT_SECRET` and manage them yourself.

## In transit

Kowan serves plain HTTP on port 5081 unless you give it a certificate with `KOWAN_TLS_CERT` and `KOWAN_TLS_KEY`. In most setups, put it behind a reverse proxy that terminates HTTPS. See [Installation](installation.md).

Kowan's responses carry a strict Content Security Policy, forbid framing, and send no referrer. Kowan adds HSTS when it serves HTTPS itself or when `KOWAN_EXTERNAL_URL` starts with `https://`.

## Network allowlists

Allowlists limit by IP address who can reach the console, the API and AI assistants, which addresses edge agents connect from, and who can connect to an agent in direct mode. Only trusted reverse proxies can report a client's address. See [Network access](network-access.md).

## Scripts on the server

Workflow script steps run bash on the Kowan server with a minimal environment. Only admins can create them. If you do not use them, turn them off with `KOWAN_SERVER_SCRIPTS=off`. See [Workflows and webhooks](workflows-and-webhooks.md).

## Audit log

Every change through the console, the API, `kowanctl` or an AI assistant, and every sign-in attempt, is recorded with the user, time, status and IP address. See [Audit log](audit-log.md).

## Hardening checklist

1. Serve Kowan over HTTPS and set `KOWAN_EXTERNAL_URL`.
2. Restrict who can reach it with `KOWAN_UI_ALLOW_CIDRS`, and list your proxy in `KOWAN_TRUSTED_PROXIES`.
3. Give each person the lowest role they need, and use teams to scope operators.
4. Use single sign-on with SSO-only mode, and keep a break-glass admin with a strong password.
5. Give each script or CI job its own API key, with the lowest role and a team or scopes.
6. Set `KOWAN_AGENT_ALLOW_CIDRS` on agents in direct mode.
7. Turn off server scripts with `KOWAN_SERVER_SCRIPTS=off` if you do not need them.
8. Back up the data volume and store the backups securely.
9. Keep Kowan up to date. See [Upgrading](upgrading.md).

## Report a vulnerability

Report security issues privately through GitHub's private vulnerability reporting: on the Kowan repository, open **Security → Report a vulnerability**, or go straight to the [report form](https://github.com/kowan-lab/kowan/security/advisories/new).

Include the affected version, how to reproduce the issue and its impact. You will get an acknowledgement within a few days. The fix is prepared in a private advisory and released together with the disclosure.

Kowan is pre-1.0, so security fixes go into the latest release only.
