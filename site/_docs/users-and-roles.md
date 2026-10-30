---
title: Users, roles and teams
description: Add users to Kowan, give each one the admin, operator or viewer role, and use teams and grants to limit access to specific hosts, stacks or deployments.
---

Everyone who signs in to Kowan has a user account with one of three roles. On top of the roles, teams and grants let you limit a user to specific servers, deployments, Compose stacks or labeled resources.

## Roles

| | Viewer | Operator | Admin |
|---|---|---|---|
| See servers, workloads, containers, images, volumes, networks, logs and metrics | Yes | Yes | Yes |
| Start, stop and restart containers, open a terminal, deploy and roll back | No | Yes | Yes |
| Create and edit deployments, schedules, workflows, uptime checks and backup jobs | No | Yes | Yes |
| Add and remove servers, update the server and agents | No | No | Yes |
| Manage users, teams, API keys, single sign-on and network access | No | No | Yes |
| Manage registries, credentials, secrets and notification channels | No | No | Yes |
| Read the audit log, back up and restore the server | No | No | Yes |

Viewers see the names of environment variables but not their values, so passwords in a container's configuration stay hidden from them.

!!! warning
    Operators can deploy containers, and a container can be given access to the host. Give the operator role only to people you would trust with root on the servers they can reach. See [Security](security.md).

## Add a user

1. Open **Settings → Users** and click **New user**.
2. Enter a **Username** and an initial **Password** of at least 8 characters.
3. Optionally enter an **Email**. If the person later signs in with [single sign-on](single-sign-on.md), Kowan links the identity to this account when the provider confirms the same email.
4. Pick a **Role** and click **Create**.

You do not need to create accounts for people who use single sign-on: Kowan can create them at their first sign-in.

To list users from a terminal, run `kowanctl user ls`.

## Change a role or reset a password

1. In **Settings → Users**, click the edit icon (**Edit role / reset password**) on the user's row.
2. Change the **Role** or the **Email**, or type a new password in **Reset password**. Leave it blank to keep the current password.
3. Click **Save**.

A new role or password ends that user's open sessions, so they sign in again.

Every user, whatever their role, can change their own password in **Settings → General**, under **Profile**, by entering the current password and the new one. The current browser tab stays signed in and their other sessions end.

## Delete a user

Click the delete icon on the user's row and confirm. Kowan also removes the user's team memberships, direct grants and single sign-on links. You cannot delete your own account.

## Teams and grants

Without teams, every user acts with their role on every server. Teams let you narrow that.

Grant
:   Access to one scope (for example a host, or a Compose stack) at one level: read, operate or manage.

Team
:   A group of users who share the same grants.

Scoped user
:   A user who belongs to at least one team or holds a direct grant. A scoped user can only see and act on what their grants cover.

The role stays the ceiling: a viewer with a **Manage** grant can still only read. Admins are never restricted by grants.

### Scopes

| Scope | Covers |
|---|---|
| **All hosts** | Every server and everything on it, plus fleet-wide objects such as workflows. |
| **One host** | Everything on one server: containers, images, volumes, networks and deployments. |
| **Deployment** | One deployment and the containers, volumes and networks it runs. |
| **Stack** | The containers, volumes and networks of one Compose project on one server. |
| **Label selector** | Containers, volumes and networks with a `key=value` label, on one server or on all of them. |

### Levels

| Level | Allows |
|---|---|
| **Read** | See, inspect, read logs and stats. |
| **Operate** | Read, plus start, stop, restart, pause, open a terminal, deploy and sync. |
| **Manage** | Operate, plus create, update, delete, prune, pull and build. |

### Create a team

1. Open **Settings → Teams** and click **New team**.
2. Enter a **Name** and an optional **Description**, then click **Save**.
3. Click the team in the list. In its members card, choose a user in **Add a user…** and click **Add**.
4. In its grants card, click **Add grant**. Choose a **Scope**, then the host, deployment, Compose project or label it applies to, and a **Level**. Click **Add grant**.

Changes to teams and grants apply at once, without signing in again.

!!! warning "A team without grants gives no access"
    Members of a team with no grants see nothing until you add one. Removing a user from their last team (when they hold no direct grants) gives them back their role on every server.

### Check what a user can do

In **Settings → Users**, click the lock icon (**Effective permissions**) on a user's row. Kowan shows whether the user is an admin, unscoped (their role applies everywhere) or scoped, and lists every grant that applies with the team it comes from.

### Grant access to a single user

The console assigns grants to teams. To give one user a direct grant, call the API as an admin:

```bash
curl -X POST https://kowan.example.com/api/v1/grants \
  -H "X-API-Key: $KOWAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"subjectType": "user", "subjectId": "<user id>", "scope": "host", "hostId": "<host id>", "level": "operate"}'
```

The `scope` is one of `all_hosts`, `host`, `deployment`, `stack` or `labels`; `target` holds the deployment id, the Compose project name or the `key=value` label. See [REST API](api.md).

### Limits

- The image list, system information, disk usage, Docker events and prune need a grant on the whole host (**All hosts** or **One host**).
- Schedules, volume backups and auto-update policies follow the grant on what they act on: a team member sees and changes only those for hosts, deployments and stacks they can access.
- API keys can be limited the same way. See [API keys](api-keys.md).

Every change to teams and grants is recorded in the [audit log](audit-log.md).
