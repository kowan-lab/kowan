"""App-template catalog over the protected ``/api/v1`` surface.

The catalog merges the built-in templates shipped with the package
(``builtIn: true``, immutable) with admin-created custom templates stored in the
``templates`` table. A template is a container spec or a compose file plus
``inputs`` (``${NAME}`` placeholders the UI asks for); see :mod:`kowan.templates`.

- GET    /templates              -> built-ins, then custom templates (any role)
- GET    /templates/{id}         -> one template (any role)
- POST   /templates/{id}/render  -> {name, values} -> {type, spec, compose}
                                    ready for POST /deployments (any role; it
                                    only substitutes values, it creates nothing)
- POST   /templates              -> create a custom template (admin)
- PUT    /templates/{id}         -> replace a custom template (admin)
- DELETE /templates/{id}         -> delete a custom template (admin)

Built-in ids (``builtin:*``) are refused by PUT/DELETE with 400.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from aiohttp import web
from pydantic import ValidationError

from kowan.store import NotFoundError
from kowan.templates import (
    Template,
    TemplateError,
    TemplateInput,
    builtins,
    find_builtin,
    render,
    validate_template,
)
from kowan.web import (
    api_route,
    bind_svc,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    reject_unknown_fields,
    require_role,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services


_FIELDS = {
    "name", "description", "category", "icon", "type", "spec", "compose",
    "inputs", "website",
}


def _lookup(svc: "Services", template_id: str) -> Optional[Template]:
    bi = find_builtin(template_id)
    if bi is not None:
        return bi
    try:
        return svc.store.get_template(template_id)
    except NotFoundError:
        return None


def _from_body(body: dict) -> Template:
    """Build a Template from a create/update body (raises TemplateError)."""
    reject_unknown_fields(body, _FIELDS)
    spec = body.get("spec")
    if spec is not None and not isinstance(spec, dict):
        raise TemplateError("spec must be an object")
    raw_inputs = body.get("inputs") or []
    if not isinstance(raw_inputs, list):
        raise TemplateError("inputs must be an array")
    try:
        inputs = [TemplateInput.model_validate(i) for i in raw_inputs]
    except ValidationError as err:
        raise TemplateError(f"invalid inputs: {err.errors()[0].get('msg', err)}") from err
    t = Template(
        name=str(body.get("name") or "").strip(),
        description=str(body.get("description") or ""),
        category=str(body.get("category") or ""),
        icon=str(body.get("icon") or ""),
        type=str(body.get("type") or ""),
        spec=spec,
        compose=str(body.get("compose") or ""),
        inputs=inputs,
        website=str(body.get("website") or ""),
    )
    # Keep exactly one body per type.
    if t.type == "container":
        t.compose = ""
    elif t.type == "compose":
        t.spec = None
    validate_template(t)
    return t


async def _list(svc: "Services", request: web.Request) -> web.Response:
    try:
        custom = svc.store.list_templates()
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to list templates")
    return json_response(builtins() + custom)


async def _get(svc: "Services", request: web.Request) -> web.Response:
    t = _lookup(svc, request.match_info.get("id", ""))
    if t is None:
        return error_response(404, "template not found")
    return json_response(t)


async def _render(svc: "Services", request: web.Request) -> web.Response:
    t = _lookup(svc, request.match_info.get("id", ""))
    if t is None:
        return error_response(404, "template not found")
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    reject_unknown_fields(body, {"name", "values"})
    values = body.get("values") or {}
    if not isinstance(values, dict):
        return error_response(400, "values must be an object")
    try:
        out = render(t, values, name=str(body.get("name") or t.name))
    except TemplateError as err:
        return error_response(400, str(err))
    return json_response(out)


async def _create(svc: "Services", request: web.Request) -> web.Response:
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    try:
        t = _from_body(body)
    except TemplateError as err:
        return error_response(400, str(err))
    now = now_rfc3339()
    t.id = str(uuid4())
    t.created_at = now
    t.updated_at = now
    try:
        svc.store.create_template(t)
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to create template")
    return json_response(t, status=201)


async def _update(svc: "Services", request: web.Request) -> web.Response:
    template_id = request.match_info.get("id", "")
    if find_builtin(template_id) is not None:
        return error_response(400, "built-in templates cannot be modified")
    try:
        existing = svc.store.get_template(template_id)
    except NotFoundError:
        return error_response(404, "template not found")
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    try:
        t = _from_body(body)
    except TemplateError as err:
        return error_response(400, str(err))
    t.id = existing.id
    t.created_at = existing.created_at
    t.updated_at = now_rfc3339()
    try:
        svc.store.update_template(t)
    except NotFoundError:
        return error_response(404, "template not found")
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to update template")
    return json_response(t)


async def _delete(svc: "Services", request: web.Request) -> web.Response:
    template_id = request.match_info.get("id", "")
    if find_builtin(template_id) is not None:
        return error_response(400, "built-in templates cannot be deleted")
    try:
        svc.store.delete_template(template_id)
    except NotFoundError:
        return error_response(404, "template not found")
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to delete template")
    return json_response({"status": "deleted"})


def register(app: web.Application, svc: "Services") -> None:
    """Mount the template routes. Reads + render for any authenticated user;
    custom-template mutations are admin-only."""
    h = bind_svc(svc)
    admin = require_role("admin")
    api_route(app, "GET", "/templates", h(_list))
    api_route(app, "GET", "/templates/{id}", h(_get))
    api_route(app, "POST", "/templates/{id}/render", h(_render))
    api_route(app, "POST", "/templates", admin(h(_create)))
    api_route(app, "PUT", "/templates/{id}", admin(h(_update)))
    api_route(app, "DELETE", "/templates/{id}", admin(h(_delete)))
