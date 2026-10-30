# Teams and resource-scoped permissions

Kowan keeps its three global roles and adds teams and grants on top of them.

## The model

* **Roles are the ceiling.** `viewer` can at most *read*; `operator` can at most
  *manage* (everything operators can do today); `admin` bypasses every check.
  The existing role gates on every route still apply (a viewer can never
  restart a container, whatever its grants).
* **Grants** are `subject × scope × level`:
  * subject: a **team**, a **user** (direct grant) or an **API key** (narrowed scopes);
  * scope:

    | scope       | qualifiers                       | covers |
    |-------------|----------------------------------|--------|
    | `all_hosts` | –                                | every host and everything on it, plus fleet-wide objects (workflows, global alert rules) |
    | `host`      | host                             | everything on that host: containers, images, volumes, networks, system info, events, deployments targeting it |
    | `deployment`| deployment                       | the deployment record and the containers / volumes / networks it runs (label `kowan.deployment.id` or its compose project) on its host |
    | `stack`     | host + compose project           | containers, volumes and networks labelled `com.docker.compose.project=<project>` (or a swarm stack namespace) on that host; deployments whose resource name is the project |
    | `labels`    | `key=value`, optional host       | containers, volumes and networks carrying the label (on one host or any) |
  * level: `read` < `operate` (start / stop / restart / pause / exec, deploy,
    sync) < `manage` (create, update, delete, prune, pull, build).
* **Scoped vs. unscoped.** A user becomes *scoped* as soon as they are a member
  of any team or hold a direct grant. A scoped user may only do what one of
  their grants (direct or through any of their teams) covers, at that level or
  below, capped by the role. A user in no team and without grants keeps the
  pre-teams behaviour: their role on everything. **An install without teams
  behaves exactly as before.**
* Membership in a team with no grants means *no access* (fail closed). Removing
  a user from their last team restores their global role.
* Host-level objects (images, system info / disk usage, events, prune, plugins,
  swarm nodes) need a host-wide grant (`all_hosts` or `host`). Image metadata
  (`inspect`, history) is readable by anyone who can see the host, so container
  details and update checks keep working for stack/label-scoped users.
* Running a deployment (`operate` on it) replaces its containers and pulls its
  images: during that request the deployment's host is treated as `manage` for
  the deployment code path only.

## Enforcement (one place)

`kowan/auth/authorize.py` holds the single decision function
`authorize(principal, level, resource)` and the plumbing that makes sure it is
asked:

* the auth middleware binds a per-request authorization context (REST and the
  MCP endpoint alike);
* `docker.Provider.ops(host)` — the only way any API module, MCP tool,
  deployment or workflow reaches Docker — returns a `ScopedOps` proxy for a
  scoped principal without host-wide manage. Every Ops call is checked
  (host-level, or object-level by inspecting the container / volume / network
  labels) and list calls are filtered. Unknown Ops methods fail closed.
* store-backed objects are checked/filtered with helpers: hosts
  (`filter_hosts`, `check_host`), deployments (`check_deployment`,
  `filter_deployments`), alerts / rules / notifications by host, workflows
  (fleet-wide: need an `all_hosts` grant).
* MCP tools use the same principal; `list_hosts`, `list_containers`,
  `list_deployments`, `list_alerts`, `list_workflows` and the fleet snapshot
  return only what the caller may read, and object tools fail with the same
  errors as REST.
* Background workers (monitor, reconcile loop, scheduler, webhooks) have no
  bound principal and are not restricted.

## API keys

`POST /api/v1/apikeys` accepts `teamId` (the key acts with the team's grants)
and/or `scopes` (a list of `{scope, hostId, target, level}`, stored as grants
with subject `apikey`). A team-bound or scoped key cannot have the `admin`
role. Deleting the key deletes its scopes. A team with bound keys cannot be
deleted (409, naming the keys) until they are revoked: an unbound key would act
with its full role, and keys cannot be rebound.

## REST

| method | path | |
|---|---|---|
| GET/POST | `/api/v1/teams` | list (with members + grants) / create `{name, description}` |
| GET/PUT/DELETE | `/api/v1/teams/{id}` | |
| POST | `/api/v1/teams/{id}/members` | `{userId}` |
| DELETE | `/api/v1/teams/{id}/members/{userId}` | |
| GET | `/api/v1/grants?subjectType=&subjectId=` | |
| POST | `/api/v1/grants` | `{subjectType, subjectId, scope, hostId?, target?, level}` |
| DELETE | `/api/v1/grants/{id}` | |
| GET | `/api/v1/users/{id}/permissions` | effective permissions (admin) |
| GET | `/api/v1/me/permissions` | the caller's own |

All but `/me/permissions` are admin-only. Every change is written to the audit
log (`team.create`, `team.member.add`, `grant.create`, `grant.delete`, …) with
the grant as JSON `detail`.

## Schema

`teams(id, name, description, …)`, `team_members(team_id, user_id, source,
added_at)` (`source` = `manual` or `sso:<provider id>`), `grants(id,
subject_type, subject_id, scope_type, host_id, target, level, created_at,
created_by)`, `api_keys.team_id`.

## Known limits

* Docker events streams need a host-wide grant (they are not filtered per object).
* The notification unread counter is global; the notification list is filtered.
* Store-backed objects of newer modules (backups, schedules, auto-update
  policies, GitOps) are not filtered yet; their Docker actions are enforced
  through `ScopedOps`.
* JWTs are stateless: a role change applies at the next sign-in, grant and team
  changes apply immediately.
