"""Write secret values into a deployment's compose.

  POST /deployments/{id}/inline-secrets {refs: ["shop-env.TAG", ...]}  (admin)

Each ``[[ref]]`` token of the compose is replaced by the secret's value, so
what runs does not change: no redeploy. Typical use is after an import, which
keeps a stack's variables in a secret: an image tag or a port is easier to
work with written in (updates, scans and promotion read images from the
compose). The result is a new revision, so a rollback takes it back. Admin
only, because it reveals secret values.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kowan.api.compose_validate import deployment_gate
from kowan.auth import authorize as authz
from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.compose.lint import SECRET_TOKEN
from kowan.errors import NotFound, Validation
from kowan.web import SERVICES_KEY, api_route, json_response, now_rfc3339, read_json, reject_unknown_fields

MAX_REFS = 200


def register(app: web.Application, svc) -> None:
    api_route(app, "POST", "/deployments/{id}/inline-secrets", require_role("admin")(_inline))


async def _inline(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    try:
        dep = svc.store.get_deployment(request.match_info["id"])
    except Exception as err:  # store.NotFoundError
        raise NotFound("deployment not found") from err
    authz.check_deployment(dep, authz.LEVEL_MANAGE)
    if dep.type != "compose" or dep.git is not None:
        raise Validation("only an inline compose deployment has a compose to write into")

    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"refs"})
    refs = body.get("refs")
    if not isinstance(refs, list) or not refs or len(refs) > MAX_REFS or not all(isinstance(r, str) and r for r in refs):
        raise Validation(f"refs must list 1 to {MAX_REFS} secret references")
    wanted = set(refs)
    present = {m.group(1) for m in SECRET_TOKEN.finditer(dep.compose)}
    missing = sorted(wanted - present)
    if missing:
        raise Validation(f"the compose has no [[{missing[0]}]]")

    values = {}
    for ref in sorted(wanted):
        token = f"[[{ref}]]"
        value = await asyncio.to_thread(svc.store.resolve_secrets, token)
        if value == token:
            raise Validation(f"[[{ref}]] does not resolve to a secret value")
        values[ref] = value
    compose = SECRET_TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), dep.compose)

    # A value can break the YAML around it (a ": " or a "#"); refuse rather
    # than store a compose that no longer parses.
    gate = await deployment_gate(svc, type_="compose", compose=compose, has_git=False, targets=dep.targets,
                                 deployment_id=dep.id, project=dep.resource_name())
    if gate is not None:
        return gate

    dep.compose = compose
    dep.updated_at = now_rfc3339()
    u = user_from(request)
    svc.store.update_deployment(dep, author=getattr(u, "username", "") if u else "", source="edit",
                                note=f"wrote {', '.join(sorted(wanted))} into the compose")
    return json_response(dep)
