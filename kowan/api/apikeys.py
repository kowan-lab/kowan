"""Admin-managed API keys over the protected ``/api/v1`` surface.

An API key is a role-scoped credential that authenticates API requests without a
username/password login — used by kowanctl, webhook scripts, and CI. Keys are
stored hashed (SHA-256); the full secret is shown exactly once, in the create
response, and is never persisted or serialized again. Only the display ``Prefix``
(e.g. ``pk_1a2b3c4d``) is retained.

All routes are admin-only because a key grants programmatic access at the
configured role. Listing returns metadata only (no hash, no secret).

- GET    /apikeys        -> list key metadata
- POST   /apikeys        -> create a key (returns the secret exactly once)
- DELETE /apikeys/{id}   -> revoke a key
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from aiohttp import web

from kowan.models import APIKey
from kowan.store import generate_api_key_secret, hash_api_key_secret
from kowan.web import (
    api_route,
    bind_svc,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    reject_unknown_fields,
    require_role,
    user_from,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services


# Recognized role constants (admin/operator/viewer); the
# control plane uses the plain strings, matching ``require_role("admin")`` usage.
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"


def _valid_role(role: str) -> bool:
    """Report whether ``role`` is one of the recognized role constants."""
    return role in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)


async def _list_keys(svc: "Services", request: web.Request) -> web.Response:
    """Return all API keys' metadata (no hash, no secret)."""
    try:
        keys = svc.store.list_api_keys()
        grants = svc.store.list_grants()
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to list api keys")
    for k in keys:
        k.scopes = [g.to_dict() for g in grants if g.subject_type == "apikey" and g.subject_id == k.id]
    return json_response(keys or [])


async def _create_key(svc: "Services", request: web.Request) -> web.Response:
    """Create a new API key from ``{name, role}``.

    Generates a fresh secret (``pk_`` + random hex), stores only its SHA-256 hash
    and display prefix, and returns the full secret exactly once in the response.
    After this it can never be retrieved again. ``role`` defaults to viewer.
    """
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    # Strict decode: unknown body fields are rejected.
    reject_unknown_fields(body, {"name", "role", "teamId", "scopes"})

    name = str(body.get("name", "")).strip()
    if name == "":
        return error_response(400, "name is required")

    role = str(body.get("role", "")) or ROLE_VIEWER
    if not _valid_role(role):
        return error_response(400, "invalid role")

    # Resource scoping (docs/permissions.md): bind to a team and/or narrow to
    # explicit scopes. A scoped key is never an admin key (admins bypass scopes).
    team_id = str(body.get("teamId", "") or "").strip()
    raw_scopes = body.get("scopes") or []
    if not isinstance(raw_scopes, list):
        return error_response(400, "scopes must be a list")
    if team_id:
        try:
            svc.store.get_team(team_id)
        except Exception:  # noqa: BLE001
            return error_response(400, "team not found")
    scope_fields = []
    from kowan.api.teams import validate_grant

    for sc in raw_scopes:
        if not isinstance(sc, dict):
            return error_response(400, "invalid scope")
        fields, err = validate_grant(svc.store, sc, subject_required=False)
        if err:
            return error_response(400, err)
        scope_fields.append(fields)
    if (team_id or scope_fields) and role == ROLE_ADMIN:
        return error_response(400, "a team-bound or scoped key cannot have the admin role")

    try:
        secret, prefix = generate_api_key_secret()
    except Exception as err:  # noqa: BLE001
        return error_response(500, "failed to generate api key")

    key = APIKey(
        id=str(uuid4()),
        name=name,
        prefix=prefix,
        role=role,
        createdAt=now_rfc3339(),
        teamId=team_id,
    )
    try:
        svc.store.create_api_key(key, hash_api_key_secret(secret))
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to create api key")
    if scope_fields:
        from kowan.api.teams import audit
        from kowan.store.teams import GrantRow

        u = user_from(request)
        for f in scope_fields:
            f.update(subject_type="apikey", subject_id=key.id)
            g = GrantRow(id=str(uuid4()), created_at=key.created_at,
                         created_by=getattr(u, "username", "") or "", **f)
            svc.store.create_grant(g)
            key.scopes.append(g.to_dict())
            audit(svc, request, "grant.create", g.to_dict(), 201)

    # The secret is returned exactly once here and is never retrievable again.
    return json_response(
        {"apiKey": key.model_dump(by_alias=True), "secret": secret},
        status=201,
    )


async def _delete_key(svc: "Services", request: web.Request) -> web.Response:
    """Remove an API key by id, immediately revoking it."""
    key_id = request.match_info.get("id", "")
    try:
        svc.store.delete_api_key(key_id)
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to delete api key")
    return json_response({"status": "deleted"})


def register(app: web.Application, svc: "Services") -> None:
    """Mount the API key routes under ``/api/v1``. Every route is admin-only."""
    h = bind_svc(svc)
    admin = require_role(ROLE_ADMIN)
    api_route(app, "GET", "/apikeys", admin(h(_list_keys)))
    api_route(app, "POST", "/apikeys", admin(h(_create_key)))
    api_route(app, "DELETE", "/apikeys/{id}", admin(h(_delete_key)))
