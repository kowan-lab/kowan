# Deployment history, diff and rollback

Every save that changes what a deployment runs is a numbered revision; any
two can be compared as a YAML diff, and any older one restored with one click.

Code: `kowan/deployment/revisions.py` (snapshot, diff, apply),
`kowan/store/deployments.py` (`deployment_revisions` table),
`kowan/api/deployments.py`; UI: History tab of a deployment workload
(`components/deployment/DeploymentHistory.tsx`); CLI: `kowanctl deployment
history | diff | rollback`.

## What a revision holds

The runtime-relevant snapshot: name, type, strategy, targets, replicas, the
container spec (fields left at their defaults are pruned) or the compose file,
and for a git deployment its source and the commit it ran. Status, enabled,
protection and timestamps are not part of it: toggling them records nothing.
Up to 100 revisions are kept per deployment (`revisions.KEEP`).

Each revision records who (`author`) and why (`source`): `create`, `edit`,
`git` (a deploy that ran a new commit, note `commit abc123`), `gitops`,
`exposure`, `mcp`, `rollback`, `preview`, `promote`, `import`.

## API

```
GET  /api/v1/deployments/{id}/revisions
GET  /api/v1/deployments/{id}/revisions/{n}
GET  /api/v1/deployments/{id}/revisions/{n}/diff?against=m   (default: n-1)
POST /api/v1/deployments/{id}/revisions/{n}/rollback {deploy: true}   (operator)
```

Viewers get redacted configurations (environment values, git secrets).
A rollback is a new revision, never a rewrite of history. A git revision is
restored **pinned to the commit it ran** with auto-deploy off, so the next push
does not undo it; re-enable auto-deploy on the git source to follow the branch
again. A rollback is refused when it would clash with another deployment's
name, target a host that no longer exists, or fail the compose gate.
