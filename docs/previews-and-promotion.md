# Preview environments and promotion

## Previews

A git compose deployment can run other branches and pull requests next to
itself, each as its own deployment on the same host.

Code: `kowan/deployment/previews.py`, the webhook in
`kowan/deployment/hook.py`, routes in `kowan/api/deployments.py`; UI:
`components/deployment/DeploymentPreviews.tsx` (settings, list, "New
preview", the banner on a preview's own page); CLI: `kowanctl deployment
previews | preview`.

Settings live in the git source (`git.previews`):

| field | meaning |
|---|---|
| `enabled` | previews on |
| `branches` | comma-separated globs: a push to a matching branch creates a preview |
| `pullRequests` | opening a pull/merge request creates one, closing it removes it |
| `ttlHours` | remove a preview nobody pushed to for this long (0 = never) |
| `max` | previews at once (1..20) |

The deployment's own webhook carries the events (GitHub, Gitea, GitLab;
signature verified first): a push to another branch updates every preview of
that branch and creates one when the branch matches; a deleted branch or a
closed/merged request removes its previews. Standalone deployments only.

A preview is a deployment with `preview: {parentId, ref, pullRequest, title,
url, lastActivityAt}`, named `<parent>-<branch>` or `<parent>-pr-<n>`. Before
it is deployed its compose file loses fixed host ports (`"8080:80"` becomes
`"80"`, Docker picks a free one) and `container_name`, so it never clashes
with the parent. The file can tell it runs as a preview from
`KOWAN_PREVIEW`, `KOWAN_PREVIEW_NAME`, `KOWAN_PREVIEW_BRANCH`,
`KOWAN_PREVIEW_PR`, for example a Traefik rule
`` Host(`${KOWAN_PREVIEW_NAME:-shop}.example.com`) ``. Deleting the parent
deletes its previews; an expiry pass every 10 minutes removes idle ones and
the previews of parents that turned previews off.

## Promotion

A deployment can name the deployment it promotes to (staging → production).
Promoting runs in the target what the source runs, never its environment:

- container: the image;
- inline compose: the image of every service both files have (edited in
  place, comments and layout kept; services only in the source are listed,
  not added);
- git: the exact commit the source last deployed, pinned with auto-deploy off.

Code: `kowan/deployment/promotion.py`; UI:
`components/deployment/DeploymentPromotion.tsx`; CLI: `kowanctl deployment
promote [--to] [--dry-run] [--no-deploy]`.

```
PUT  /api/v1/deployments/{id}/promotion {to}            (operator)
GET  /api/v1/deployments/{id}/promote?to=               plan: changes, notes, YAML diff
POST /api/v1/deployments/{id}/promote {to?, deploy}     (operator; a new revision of the target)
```

The target needs the "manage" permission. Different kinds (container vs
compose, git vs inline, different repositories) are refused with a reason.
