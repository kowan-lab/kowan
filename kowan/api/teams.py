"""Teams, grants and effective permissions (docs/permissions.md).

Admin only, except ``GET /me/permissions``:

  GET/POST          /teams
  GET/PUT/DELETE    /teams/{id}
  POST              /teams/{id}/members            {userId}
  DELETE            /teams/{id}/members/{userId}
  GET               /grants?subjectType=&subjectId=
  POST              /grants                        {subjectType, subjectId, scope, hostId?, target?, level}
  DELETE            /grants/{id}
  GET               /users/{id}/permissions        effective permissions of a user
  GET               /me/permissions                the caller's own

Every change is written to the audit log with a JSON ``detail``.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Optional

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.store.teams import SOURCE_MANUAL, SUBJECT_TEAM, SUBJECT_TYPES, SUBJECT_USER, GrantRow, Team
from kowan.web import (
    SERVICES_KEY,
    api_route,
    client_ip,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    require_role,
    user_from,
)

log = logging.getLogger("kowan.api.teams")


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def audit(svc: Any, request: web.Request, action: str, detail: dict, status: int = 200) -> None:
    rec = getattr(svc, "audit", None)
    if rec is None:
        return
    u = user_from(request)
    try:
        rec.record({
            "id": str(uuid.uuid4()),
            "userId": getattr(u, "id", "") or "",
            "username": getattr(u, "username", "") or "",
            "action": action,
            "path": request.rel_url.path,
            "status": status,
            "ip": client_ip(request),
            "detail": json.dumps(detail, separators=(",", ":"), sort_keys=True),
        })
    except Exception as err:  # noqa: BLE001
        log.error("audit %s: %s", action, err)


async def _body(request: web.Request) -> Optional[dict]:
    body = await read_json(request)
    return body if isinstance(body, dict) else None


# -- teams ------------------------------------------------------------------


async def _list_teams(request: web.Request) -> web.Response:
    svc = _svc(request)
    teams = svc.store.list_teams()
    grants = svc.store.list_grants()
    out = []
    for t in teams:
        d = t.to_dict()
        d["grants"] = [g.to_dict() for g in grants if g.subject_type == SUBJECT_TEAM and g.subject_id == t.id]
        out.append(d)
    return json_response(out)


async def _get_team(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        t = svc.store.get_team(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "team not found")
    d = t.to_dict()
    d["grants"] = [g.to_dict() for g in svc.store.list_grants(SUBJECT_TEAM, t.id)]
    return json_response(d)


def _clean_name(v: Any) -> str:
    return str(v or "").strip()[:80]


async def _create_team(request: web.Request) -> web.Response:
    svc = _svc(request)
    body = await _body(request)
    if body is None or set(body) - {"name", "description"}:
        return error_response(400, "invalid request body")
    name = _clean_name(body.get("name"))
    if not name:
        return error_response(400, "name is required")
    try:
        svc.store.get_team_by_name(name)
        return error_response(409, "a team with that name already exists")
    except Exception:  # noqa: BLE001
        pass
    now = now_rfc3339()
    t = Team(id=str(uuid.uuid4()), name=name, description=str(body.get("description") or "")[:500],
             created_at=now, updated_at=now)
    svc.store.create_team(t)
    audit(svc, request, "team.create", {"team": t.id, "name": t.name}, 201)
    return json_response(svc.store.get_team(t.id).to_dict(), status=201)


async def _update_team(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        t = svc.store.get_team(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "team not found")
    body = await _body(request)
    if body is None or set(body) - {"name", "description"}:
        return error_response(400, "invalid request body")
    if "name" in body:
        name = _clean_name(body.get("name"))
        if not name:
            return error_response(400, "name is required")
        if name.lower() != t.name.lower():
            try:
                svc.store.get_team_by_name(name)
                return error_response(409, "a team with that name already exists")
            except Exception:  # noqa: BLE001
                pass
        t.name = name
    if "description" in body:
        t.description = str(body.get("description") or "")[:500]
    t.updated_at = now_rfc3339()
    svc.store.update_team(t)
    audit(svc, request, "team.update", {"team": t.id, "name": t.name})
    return json_response(svc.store.get_team(t.id).to_dict())


async def _delete_team(request: web.Request) -> web.Response:
    svc = _svc(request)
    tid = request.match_info["id"]
    try:
        t = svc.store.get_team(tid)
    except Exception:  # noqa: BLE001
        return error_response(404, "team not found")
    svc.store.delete_team(tid)
    audit(svc, request, "team.delete", {"team": tid, "name": t.name})
    return json_response({"status": "deleted"})


async def _add_member(request: web.Request) -> web.Response:
    svc = _svc(request)
    tid = request.match_info["id"]
    try:
        svc.store.get_team(tid)
    except Exception:  # noqa: BLE001
        return error_response(404, "team not found")
    body = await _body(request)
    if body is None or set(body) - {"userId"}:
        return error_response(400, "invalid request body")
    uid = str(body.get("userId") or "")
    try:
        u = svc.store.get_user(uid)
    except Exception:  # noqa: BLE001
        return error_response(400, "user not found")
    added = svc.store.add_team_member(tid, uid, SOURCE_MANUAL, now_rfc3339())
    audit(svc, request, "team.member.add", {"team": tid, "user": uid, "username": u.username})
    return json_response(svc.store.get_team(tid).to_dict(), status=201 if added else 200)


async def _remove_member(request: web.Request) -> web.Response:
    svc = _svc(request)
    tid, uid = request.match_info["id"], request.match_info["userId"]
    svc.store.remove_team_member(tid, uid)
    audit(svc, request, "team.member.remove", {"team": tid, "user": uid})
    return json_response({"status": "removed"})


# -- grants -----------------------------------------------------------------


def validate_grant(store: Any, body: dict, *, subject_required: bool = True) -> tuple:
    """Return ``(GrantRow-kwargs, error)`` for a grant request body."""
    allowed = {"subjectType", "subjectId", "scope", "hostId", "target", "level"}
    if set(body) - allowed:
        return None, "unknown field: " + ", ".join(sorted(set(body) - allowed))
    stype = str(body.get("subjectType") or "")
    sid = str(body.get("subjectId") or "")
    if subject_required:
        if stype not in SUBJECT_TYPES or not sid:
            return None, "subjectType (team|user|apikey) and subjectId are required"
        try:
            if stype == SUBJECT_TEAM:
                store.get_team(sid)
            elif stype == SUBJECT_USER:
                store.get_user(sid)
            else:
                store.get_api_key(sid)
        except Exception:  # noqa: BLE001
            return None, f"{stype} not found"
    scope = str(body.get("scope") or "")
    level = str(body.get("level") or "")
    host_id = str(body.get("hostId") or "").strip()
    target = str(body.get("target") or "").strip()
    if scope not in authz.SCOPES:
        return None, "scope must be one of " + ", ".join(authz.SCOPES)
    if not authz.valid_level(level):
        return None, "level must be read, operate or manage"

    def host_exists(h: str) -> bool:
        try:
            store.get_host(h)
            return True
        except Exception:  # noqa: BLE001
            return False

    if scope == authz.SCOPE_ALL_HOSTS:
        host_id, target = "", ""
    elif scope == authz.SCOPE_HOST:
        if not host_id or not host_exists(host_id):
            return None, "hostId must name an existing host"
        target = ""
    elif scope == authz.SCOPE_DEPLOYMENT:
        try:
            store.get_deployment(target)
        except Exception:  # noqa: BLE001
            return None, "target must be an existing deployment id"
        host_id = ""
    elif scope == authz.SCOPE_STACK:
        if not host_id or not host_exists(host_id):
            return None, "hostId must name an existing host"
        if not target or len(target) > 128:
            return None, "target must be the compose project name"
    elif scope == authz.SCOPE_LABELS:
        try:
            k, v = authz.parse_selector(target)
        except ValueError as err:
            return None, str(err)
        target = f"{k}={v}"
        if host_id and not host_exists(host_id):
            return None, "hostId must name an existing host"
    return {"subject_type": stype, "subject_id": sid, "scope_type": scope, "host_id": host_id,
            "target": target, "level": level}, None


async def _list_grants(request: web.Request) -> web.Response:
    svc = _svc(request)
    stype = request.query.get("subjectType", "")
    sid = request.query.get("subjectId", "")
    return json_response([g.to_dict() for g in svc.store.list_grants(stype, sid)])


async def _create_grant(request: web.Request) -> web.Response:
    svc = _svc(request)
    body = await _body(request)
    if body is None:
        return error_response(400, "invalid request body")
    fields, err = validate_grant(svc.store, body)
    if err:
        return error_response(400, err)
    u = user_from(request)
    g = GrantRow(id=str(uuid.uuid4()), created_at=now_rfc3339(), created_by=getattr(u, "username", "") or "",
                 **fields)
    svc.store.create_grant(g)
    audit(svc, request, "grant.create", g.to_dict(), 201)
    return json_response(g.to_dict(), status=201)


async def _delete_grant(request: web.Request) -> web.Response:
    svc = _svc(request)
    gid = request.match_info["id"]
    try:
        g = svc.store.get_grant(gid)
    except Exception:  # noqa: BLE001
        return error_response(404, "grant not found")
    svc.store.delete_grant(gid)
    audit(svc, request, "grant.delete", g.to_dict())
    return json_response({"status": "deleted"})


# -- effective permissions --------------------------------------------------


def effective_permissions(svc: Any, user: Any) -> dict:
    """Describe what ``user`` (a User model) may do: role ceiling, scoping and
    every grant that applies with where it comes from."""
    ctx_user = SimpleNamespace(id=user.id, username=user.username, role=user.role, key_hash="")
    p = authz.load_principal(svc.store, ctx_user)
    teams = {t.id: t.name for t in svc.store.list_teams()}
    hosts = {}
    try:
        hosts = {h.id: h.name for h in svc.store.list_hosts()}
    except Exception:  # noqa: BLE001
        pass
    deps = {}
    try:
        deps = {d.id: d.name for d in svc.store.list_deployments()}
    except Exception:  # noqa: BLE001
        pass
    grants = []
    ceiling = authz.role_ceiling(user.role)
    for g in p.grants:
        d = g.to_dict()
        src = g.source
        d["sourceName"] = ("team " + teams.get(src[5:], src[5:])) if src.startswith("team:") else "direct"
        d["hostName"] = hosts.get(g.host_id, "")
        d["targetName"] = deps.get(g.target, "") if g.scope == authz.SCOPE_DEPLOYMENT else ""
        d["effectiveLevel"] = g.level if authz.level_rank(g.level) <= authz.level_rank(ceiling) else ceiling
        grants.append(d)
    if p.is_admin:
        mode = "admin"
    elif p.scoped:
        mode = "scoped"
    else:
        mode = "global"
    return {
        "userId": user.id,
        "username": user.username,
        "role": user.role,
        "ceiling": "all" if p.is_admin else ceiling,
        "mode": mode,
        "scoped": p.scoped and not p.is_admin,
        "teams": svc.store.user_memberships(user.id),
        "grants": grants,
    }


async def _user_permissions(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        u = svc.store.get_user(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "user not found")
    out = effective_permissions(svc, u)
    try:
        out["identities"] = svc.store.list_user_identities(u.id)
    except Exception:  # noqa: BLE001
        out["identities"] = []
    return json_response(out)


async def _my_permissions(request: web.Request) -> web.Response:
    svc = _svc(request)
    cu = user_from(request)
    if cu is None:
        return error_response(401, "not authenticated")
    if getattr(cu, "key_hash", ""):
        ctx = authz.current()
        p = ctx.principal if ctx is not None else authz.load_principal(svc.store, cu)
        return json_response({"username": cu.username, "role": cu.role, "mode": "admin" if p.is_admin else (
            "scoped" if p.scoped else "global"), "scoped": p.scoped, "grants": [g.to_dict() for g in p.grants],
            "teams": []})
    try:
        u = svc.store.get_user(cu.id)
    except Exception:  # noqa: BLE001
        return error_response(404, "user not found")
    return json_response(effective_permissions(svc, u))


def register(app: web.Application, svc: Any) -> None:
    admin = require_role("admin")
    api_route(app, "GET", "/teams", admin(_list_teams))
    api_route(app, "POST", "/teams", admin(_create_team))
    api_route(app, "GET", "/teams/{id}", admin(_get_team))
    api_route(app, "PUT", "/teams/{id}", admin(_update_team))
    api_route(app, "DELETE", "/teams/{id}", admin(_delete_team))
    api_route(app, "POST", "/teams/{id}/members", admin(_add_member))
    api_route(app, "DELETE", "/teams/{id}/members/{userId}", admin(_remove_member))
    api_route(app, "GET", "/grants", admin(_list_grants))
    api_route(app, "POST", "/grants", admin(_create_grant))
    api_route(app, "DELETE", "/grants/{id}", admin(_delete_grant))
    api_route(app, "GET", "/users/{id}/permissions", admin(_user_permissions))
    api_route(app, "GET", "/me/permissions", _my_permissions)
