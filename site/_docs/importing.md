---
title: Importing stacks
description: Move Docker Compose stacks from Portainer or a folder of compose files into Kowan as deployments, with their variables, and take over the running containers.
---

Bring stacks that already run elsewhere under Kowan: from Portainer, or from a folder of Compose stacks such as `/opt/stacks` or a Dockge folder. Each stack becomes a [deployment](deployments.md), created but not deployed, so nothing on your hosts changes until you choose to deploy.

## How a running stack is taken over

Kowan recognizes a Compose deployment's containers by their Compose project name, which is the deployment's name in lowercase (unsupported characters become `-`). When you keep the name the import suggests, which is the stack's name, the new deployment matches the containers that already run under that project. They appear under the deployment right away, and deploying it runs `docker compose up` for that same project: Compose reuses the project's named volumes and recreates only the containers whose configuration changed.

If you rename the stack during the import, the deployment gets a new project name: deploying it starts a second copy next to the old containers.

!!! warning "Relative paths"
    Kowan hands the Compose file to the host as text, so relative paths in it, such as a `./data` bind mount or an `env_file`, no longer point to the stack's original folder. [Compose validation](compose-validation.md) warns about them. Before you deploy an imported stack, change them to absolute paths (such as `/opt/stacks/app/data`), named volumes or stack variables.

After the import, edit and deploy the stack from Kowan. A Kowan deploy applies Kowan's copy of the file, so it replaces changes made to the stack in Portainer or in the original folder.

## Migrate what already runs (from Docker)

You do not need Portainer to bring a stack or a container under Kowan: Kowan reads it from Docker. Open the workload and choose **Migrate to Kowan**, or go to **Import stacks → From Docker**, which lists every stack and container on your hosts that Kowan does not manage yet (Kowan's own containers excepted).

- **A Compose stack** whose files are still on its host: Compose records the project's folder and files on its containers, and Kowan reads them there with a short-lived helper container that mounts the folder read-only (only `.yml`/`.yaml` files and the `.env`). The `.env` becomes stack variables, and relative bind paths such as `./data` become absolute paths in that folder. A stack that `build`s or uses `env_file` is flagged, since those still depend on the folder.
- **A stack whose files are gone, or a Swarm stack**: Kowan writes a Compose file from what Docker reports about each container (image, command, environment, ports, volumes, networks, restart policy, limits, health check, labels), leaving out what the image already sets. Existing volumes and networks are declared `external` under their real names, so the data stays. For a Swarm stack, review the `deploy` sections: placement, update and resource settings are not visible from a container.
- **A lone container** becomes a container deployment with its own settings.

Values of variables whose names look secret (`PASSWORD`, `TOKEN`, `KEY`…) move to stack variables stored encrypted, referenced as `${NAME}`. Review the file and the variables, keep the name, and choose **Deploy now** to have Kowan take over at once (the containers are recreated: a short restart), or deploy later.

## Import from Portainer

You need the admin role.

1. Open **Workloads → New workload → Import stacks** and select **From Portainer**.
2. Pick a **Portainer connection**, or create one on the spot. It holds Portainer's address and either an access token (in Portainer: **My account → Access tokens**) or a username and password. For a self-signed certificate, set **Accept a self-signed certificate** to `true`. The connection is stored encrypted in **Settings → Credentials**, so later imports need nothing typed again.
3. Click **Find stacks**. Kowan reads Portainer's environments, its stacks and each stack's Compose file through Portainer's API.
4. Review the list (see [Choose what to import](#choose-what-to-import)) and click **Import N**.

What each kind of Portainer stack becomes:

| Portainer stack | In Kowan |
| --- | --- |
| Compose stack | An inline Compose deployment. The file comes back as written, with its `${VAR}` references, and the stack's environment becomes its stack variables. |
| Swarm stack | A Compose deployment with the **Swarm cluster** strategy. |
| Stack from a Git repository | A [Git deployment](deployments.md#deploy-from-a-git-repository) of the same repository and branch. For a private repository, pick a Git credential after importing. Its Portainer environment variables are not imported: add them to the repository's `.env`. |
| Kubernetes stack | Listed, but not importable. |

Kowan suggests the host whose name matches the Portainer environment's name, and otherwise the first host. Stacks stopped in Portainer are marked **stopped in Portainer**.

## Import compose folders

You need the operator role.

1. Open **Workloads → New workload → Import stacks** and select **From compose folders**.
2. Click **Choose folder** and pick the folder that holds your stacks, one sub-folder per stack.
3. Review the list and click **Import N**.

Your browser reads only the Compose files (`compose.yaml`, `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`) and `.env` files, up to 1 MiB each, and sends those to Kowan. Each folder with a Compose file becomes one stack, named after the folder; its `.env` becomes the stack variables. A single import reads at most 500 files.

## Choose what to import

The **Stacks found** list shows, for each stack:

- a checkbox to include it;
- its name, which becomes the deployment name (keep it to take over the running containers);
- where it comes from, and how many variables it has;
- the host it will run on;
- warnings, such as **a deployment with this name exists already**.

### Variables and secrets

Each stack variable is imported as config or as secret. Kowan shows the names, never the values, and ticks the ones that look sensitive: names containing `PASS`, `PWD`, `SECRET`, `TOKEN`, `KEY`, `CREDENTIAL`, `AUTH`, `SALT`, `PRIVATE`, `DSN` or `CONNECTION_STRING`, and URLs that carry a user and password. Change the ticks as needed.

A secret variable is stored encrypted in a secret the deployment owns, exactly like a value marked **Secret** in the deployment form; a config variable is stored as written. See [Stack variables and secrets](deployments.md#stack-variables-and-secrets).

## After importing

Each imported stack is a deployment in the stopped state, with an `import` entry in its [history](revisions-and-rollback.md). The results list links to each one. For every deployment:

1. Open it and check the **Configuration** tab: host, Compose file, stack variables and, for a Git deployment, the credential.
2. Click **Deploy** when ready.

Each import goes through the same checks as creating a deployment by hand, including [Compose validation](compose-validation.md) on the target host. A stack that fails them shows the reason in the results, and the others are imported anyway.
