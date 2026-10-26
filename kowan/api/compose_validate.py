"""Compose validation REST endpoint + the deployment gate.

- POST /compose/validate (operator) ``{compose, hostId?, deploymentId?, env?}``
  -> ``{diagnostics: [...], engine, checks, summary}``. Never 4xx for a bad
  compose file: problems are diagnostics. See :mod:`kowan.compose`.

:func:`deployment_gate` is what ``kowan/api/deployments.py`` calls on create,
update and deploy: it validates an inline compose file for the deployment's
target host and returns a ``400 {error, diagnostics}`` response when there is
at least one ``error`` diagnostic (warnings never block). Setting
``KOWAN_COMPOSE_VALIDATE=0`` disables the gate (escape hatch).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from aiohttp import web

from kowan.auth import require_role
from kowan.compose import validate_for_target
from kowan.web import SERVICES_KEY, api_route, json_response, read_json

log = logging.getLogger("kowan.api.compose_validate")


def register(app: web.Application, svc: Any) -> None:
    api_route(app, "POST", "/compose/validate", require_role("operator")(_validate))


def _project_for(svc: Any, deployment_id: str) -> str:
    if not deployment_id:
        return ""
    try:
        return svc.store.get_deployment(deployment_id).resource_name()
    except Exception:  # noqa: BLE001 - unknown deployment: no exclusion
        return ""


async def _validate(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    body = await read_json(request)
    if not isinstance(body, dict):
        return json_response({"error": "invalid request body"}, status=400)
    compose = body.get("compose")
    if not isinstance(compose, str):
        return json_response({"error": "compose must be a string"}, status=400)
    if len(compose) > 512 * 1024:
        return json_response({"error": "compose is too large"}, status=400)
    host_id = str(body.get("hostId") or "")
    dep_id = str(body.get("deploymentId") or "")
    env_raw = body.get("env")
    env: Optional[Dict[str, str]] = None
    if isinstance(env_raw, dict):
        env = {str(k): str(v) for k, v in env_raw.items()}
    res = await validate_for_target(
        svc, compose, host_id=host_id, deployment_id=dep_id,
        project=_project_for(svc, dep_id), env=env,
    )
    return json_response(res.to_dict())


def gate_enabled() -> bool:
    return os.environ.get("KOWAN_COMPOSE_VALIDATE", "1").strip().lower() not in ("0", "false", "no", "off")


async def deployment_gate(
    svc: Any,
    *,
    type_: str,
    compose: str,
    has_git: bool,
    targets: Any,
    deployment_id: str = "",
    project: str = "",
    env: Optional[Dict[str, str]] = None,
) -> Optional[web.Response]:
    """``None`` when the deployment may proceed, else a 400 response. ``env``:
    the stack variables, known to be set when the compose is deployed."""
    if not gate_enabled() or type_ != "compose" or has_git or not (compose or "").strip():
        return None
    host_id = targets[0] if targets else ""
    try:
        res = await validate_for_target(
            svc, compose, host_id=host_id, deployment_id=deployment_id, project=project, env=env or None,
        )
    except Exception as err:  # noqa: BLE001 - never let the validator break CRUD
        log.warning("compose validation failed unexpectedly: %s", err)
        return None
    errors = res.errors()
    if not errors:
        return None
    first = errors[0]
    where = f" (line {first.line})" if first.line else ""
    more = f"; {len(errors) - 1} more error(s)" if len(errors) > 1 else ""
    payload = res.to_dict()
    payload["error"] = f"compose validation failed: {first.message}{where}{more}"
    return json_response(payload, status=400)
