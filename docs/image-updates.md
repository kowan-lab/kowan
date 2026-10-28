# Image updates

Kowan answers two different questions about the images a workload runs.

| Question | Example | Where | Effect |
| --- | --- | --- | --- |
| Was the image behind this tag pushed again? | `nginx:latest`, `api:2.0` rebuilt | **Update · N** in the workload header | the same configuration, newer image |
| Does the registry have a newer version tag? | `api:2.0` → `api:2.1` | **New versions · N** in the workload header | the deployment is edited (a new revision) |

## Update · N (same tag, newer image)

The update check compares the digest of each container's image with the one
its registry serves for the tag (`kowan/api/updates.py`, cached, with the
stored registry credentials, ECR included). The button appears when at least
one service of the workload has a newer image, and updates them all at once:

- **A Kowan deployment** is updated as a unit: the changed images are pulled
  (with the stored credentials), gated by the scan policy, the deployment is
  redeployed once, every changed service is verified and, if one does not come
  up healthy, rolled back to its previous image. This is the path an `apply`
  auto-update policy takes (`AutoUpdater.update_deployment_now`), without its
  maintenance window or minimum age, and it retries a version that was rolled
  back before. `POST /api/v1/deployments/{id}/update`.
- **A compose project or swarm stack Kowan does not manage**: each container
  with a newer image is recreated with its own configuration on the new image,
  and rolled back if it does not come up (Kowan has no compose file to
  redeploy). `POST /api/v1/hosts/{hostID}/stacks/{project}/update`.
- **A lone container**: the same per-container update as before.

The **Update** button of a single container that belongs to a Kowan deployment
updates its whole deployment instead: recreating the container alone would be
undone by the next deploy.

Answers: 200 updated or already up to date, 409 blocked by the scan policy (or
an update already running), 502 rolled back or failed; a stack answers 207
when some containers failed.

## New versions · N (newer version tags)

For a deployment that is not git-sourced (a git one takes its images from the
repository), Kowan lists the tags of each service's image in its registry and
offers the newer versions of the same shape (`kowan/deployment/tagupdates.py`):

- a version tag is numbers separated by dots, with an optional `v` before and
  a suffix after: `1.27`, `v2.3.1`, `1.27-alpine`;
- only tags with the same `v`, the same number of parts and the same suffix
  count: `1.27` is offered `1.28` (not `1.28.1`), `1.27-alpine` stays alpine;
- `latest`, digests and images taken from a variable are left out, with the
  reason shown.

The dialog shows, before anything changes, **what will change**: each service
with its current tag struck through and the new one, and the line diff of the
deployment's configuration. Applying saves a new revision (source `tags`, note
`new versions: web 1.27 → 1.29`) and redeploys unless unticked; History can
roll it back.

```
GET  /api/v1/deployments/{id}/tag-updates                  per service: current, newer[], latest, note|error
POST /api/v1/deployments/{id}/tag-updates/preview  {tags}  -> {changes, diff}   (operator)
POST /api/v1/deployments/{id}/tag-updates  {tags, deploy}  -> {changes, deployment} (operator)
```

`tags` maps a service to its new tag; a container deployment has one service,
keyed `""`.

## Pulling an image

**Images → Pull image** asks for the registry first:

- **Detect from the image name** (default): type a reference; the dialog says
  which stored registry's credentials the pull will use (the one whose host
  serves the image, Docker Hub for a short name), or that it is anonymous.
- **A stored registry**: pick a repository from its catalog (or type one when
  the registry does not list them, as Docker Hub) and a tag; the reference is
  built for you.
