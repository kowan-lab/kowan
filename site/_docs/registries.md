---
title: Registries
description: Add private container registries to Kowan, such as GHCR, ECR, Artifact Registry or ACR, browse their images, and deploy your own registry with HTTPS.
---

A registry entry tells Kowan how to log in to a container registry, so it can pull private images when it deploys, check them for updates and browse their tags. You can also deploy your own registry on one of your hosts, published on a domain with a Let's Encrypt certificate.

Registries are managed by admins in **Settings → Registries**.

## Add a registry

1. Open **Settings → Registries** and click **New registry**.
2. Enter a **Name**, such as `GitHub packages`.
3. Enter the **Registry host**: `docker.io`, `ghcr.io`, `registry.example.com:5000` or your cloud registry's host.
4. Pick a **Credential**, or click **New credential** to create one. Choose **None (no credential)** for anonymous access.
5. Click **Save**. Kowan tries to log in and marks the registry **Verified** when it can.

The credential lives in **Settings → Credentials**, so several registries, or other features, can share one. The registry's type follows from the credential:

| Type | Host | Credential |
| --- | --- | --- |
| Generic (Docker v2) | `docker.io`, `registry.example.com:5000` | A username and a password or token |
| GitHub Container Registry | `ghcr.io` | A GitHub username and a personal access token |
| Amazon ECR (beta) | `<account>.dkr.ecr.<region>.amazonaws.com` | An AWS connection: region, access key ID and secret access key |
| Google Artifact Registry (beta) | `<region>-docker.pkg.dev` or `gcr.io` | A Google Cloud connection: a service account JSON key |
| Azure Container Registry (beta) | `<registry>.azurecr.io` | An Azure connection: tenant ID, client ID and client secret |

For ECR, Artifact Registry and ACR, Kowan exchanges the cloud identity for a short-lived registry token on each pull. You never run `docker login` on your hosts.

## Which registry a pull uses

Kowan picks the registry entry by the host in the image reference:

| Image | Registry host |
| --- | --- |
| `nginx:1.27` | `docker.io` |
| `ghcr.io/acme/api:2.0` | `ghcr.io` |
| `registry.example.com:5000/shop/web:1.4` | `registry.example.com:5000` |

When no entry matches, the pull is anonymous. The same credentials are used everywhere Kowan pulls or reads an image: [deployments](deployments.md), new containers, **Images → Pull image**, the update checks and automatic updates of [Image updates](image-updates.md), and the newer version tags it offers.

**Images → Pull image** shows which registry's credentials a pull will use. Keep **Detect from the image name** to let Kowan choose, or pick **A stored registry** to choose a repository and tag from its catalog.

## Check and browse a registry

Each row in **Settings → Registries** has actions to:

- **Test** the login again. The status turns **Verified**, or **Unverified** with the reason.
- **Explore** the registry in the **Registry Explorer**: browse its repositories, list each repository's tags and copy a pull reference.
- **Edit** or **Delete** the entry. Deleting an entry does not touch any image.

## Deploy your own registry

Kowan can run a private image registry (the official `registry:3` image) on one of your hosts and add it to the list.

### Before you start

- A domain whose DNS points at the host, such as `registry.example.com`.
- Ports 80 and 443 of the host open to the internet, so Let's Encrypt can issue the certificate.
- Traefik running on the host. If there is none, the dialog offers to deploy it, as on the **Exposure** page. See [Exposure and HTTPS](exposure-and-https.md).

A registry reachable only on a private network, with no public domain, is not supported: no public authority issues certificates for private addresses.

### Deploy it

1. Open **Settings → Registries** and click **Deploy a registry**.
2. Pick the **Host**, and a **Name** (default `registry`; lowercase letters, digits, `-` and `_`). It is also its name in the registry list.
3. Enter the **Domain**.
4. Leave **Require a login** on, and optionally set the **Username** (default `kowan`) and a **Password** of at least 8 characters. Leave the password empty to have Kowan generate one.
5. Click **Deploy**.

The dialog then shows the address to use in image references and the login. The password is shown only once: copy it now. Kowan keeps it, encrypted, with the registry entry for its own pulls.

To push an image from your machine:

```bash
docker login registry.example.com
docker tag myapp:1.0 registry.example.com/myapp:1.0
docker push registry.example.com/myapp:1.0
```

!!! warning
    Without a login, anyone who can reach the registry can push and pull images.

### What Kowan creates

- A Compose [deployment](deployments.md) named after the registry, with a named volume for the images and the Traefik labels for the domain. No port is published on the host, and deleting images through the registry API is enabled.
- With a login, a volume `kowan-registry-<name>-config` that holds the password file. Kowan fills it before the deploy, so the password hash is not in the Compose file.
- A registry entry for `https://<domain>`, with the login. It turns **Verified** once the registry answers.

Deleting the deployment leaves the registry entry and both volumes. Remove them by hand when you no longer need the images.

## Registries in GitOps and the API

Registries can be kept in Git with the `Registry` kind of [GitOps](gitops.md). Passwords are never stored in Git: they stay in Kowan, or come from the referenced credential.

To deploy a registry from the API, send `POST /api/v1/hosts/{hostID}/registry` as an admin:

```json
{ "name": "registry", "domain": "registry.example.com", "auth": true, "username": "kowan", "password": "" }
```

Add `proxyId` when the host runs more than one Traefik. The answer includes the URL, the address for image references and, once, the generated password. See [REST API](api.md).
