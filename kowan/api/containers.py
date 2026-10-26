"""Container REST handlers scoped per host.

Routes, roles, status codes and validation:

  GET    /hosts/{hostID}/containers?all=          list containers
  GET    /hosts/{hostID}/containers/{id}          inspect (raw doc)
  GET    /hosts/{hostID}/containers/{id}/spec     reconstruct ContainerSpec
  POST   /hosts/{hostID}/containers               create (operator)
  POST   /hosts/{hostID}/containers/{id}/start    (operator)
  POST   /hosts/{hostID}/containers/{id}/stop     (operator)
  POST   /hosts/{hostID}/containers/{id}/restart  (operator)
  POST   /hosts/{hostID}/containers/{id}/pause    (operator)
  POST   /hosts/{hostID}/containers/{id}/unpause  (operator)
  DELETE /hosts/{hostID}/containers/{id}?force=   (operator)

The host is chosen via the {hostID} path parameter: the record is looked up in
``svc.store.get_host`` and turned into a concrete :class:`~kowan.docker.Ops`
through ``svc.docker.ops(host)``. On lookup/resolution failure the same status
codes as every other per-host module are returned (400 missing id, 404 not
found, 502 resolve failure).

Read/inspect routes are public within the authenticated /api/v1 group (any
role); mutating routes require the operator role. Docker/agent failures
surface as 502 Bad Gateway.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from aiohttp import web

from .. import registry
from ..auth import authorize as authz
from ..redact import MASK, redact_inspect
from ..docker import Ops
from ..models import ContainerSpec, PortMapping
from ..web import (
    ops_error_response,
    SERVICES_KEY,
    api_route,
    error_response,
    json_response,
    read_json,
    require_role,
)

log = logging.getLogger("kowan.api.containers")

# Operator role gate for mutating routes. The
# require_role helper always also allows admins.
_OPERATOR = "operator"


# ---------------------------------------------------------------------------
# Truthy query parsing
# ---------------------------------------------------------------------------


def _is_truthy(v: Optional[str]) -> bool:
    """Report whether a query-string value should be treated as true.

    Accepts the common forms "1", "true", "yes", "on" (case-insensitive); an
    empty/absent value is false.
    """
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_ports(ports: List[PortMapping]) -> Optional[str]:
    """Reject obviously invalid port mappings before they reach the daemon.

    A container port is required and must be 1-65535; a host port of 0 means
    "publish on a random host port" and is allowed, but a negative or
    out-of-range host port is not. Returns a human-friendly error message on
    failure, or ``None`` when the mappings are valid.
    """
    for p in ports:
        if p.container_port < 1 or p.container_port > 65535:
            return (
                "container port must be between 1 and 65535 "
                f"(got {p.container_port})"
            )
        if p.host_port < 0 or p.host_port > 65535:
            return f"host port must be between 0 and 65535 (got {p.host_port})"
    return None


def _classify_create_error(err: Exception) -> Tuple[int, str]:
    """Map a create-container error to an HTTP status and a friendlier message.

    Docker rejects bad client input (invalid port specs, host-port conflicts)
    with daemon errors that are really 4xx conditions, not the 502 an
    unreachable daemon warrants. We detect the common input-error shapes and
    return 409 (conflict) or 400 (bad request); anything else is treated as an
    upstream failure (502).
    """
    msg = str(err)
    low = msg.lower()
    if "port is already allocated" in low or "address already in use" in low:
        return 409, "host port is already in use; choose a different host port"
    if "invalid port specification" in low or "invalid port" in low:
        return (
            400,
            "invalid port mapping; host and container ports must be valid "
            "port numbers",
        )
    if "no such image" in low or ("not found" in low and "image" in low):
        return (
            400,
            "image not found; pull the image or enable 'pull image' before "
            "creating",
        )
    return 502, msg


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


def _spec_from_body(body: dict) -> ContainerSpec:
    """Build a ContainerSpec from a raw JSON body (accepts camelCase aliases)."""
    # model_validate honours the field aliases (populate_by_name=True), so a
    # camelCase JSON payload maps onto the snake_case attributes.
    return ContainerSpec.model_validate(body)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(app: web.Application, svc: Any) -> None:
    """Mount the container routes under /api/v1 (see module docstring)."""

    # -- host / ops resolution ----------------------------------------------

    def _resolve_ops(request: web.Request) -> Tuple[Optional[Ops], Optional[web.Response]]:
        """Resolve the docker Ops for the {hostID} path param.

        Returns ``(ops, None)`` on success or ``(None, response)`` with an error
        response the caller must return immediately: 400 missing id, 404 host
        not found, 502 resolve failure.
        """
        host_id = request.match_info.get("hostID", "")
        if host_id == "":
            return None, error_response(400, "missing host id")

        try:
            host = svc.store.get_host(host_id)
        except Exception:
            return None, error_response(404, "host not found")

        try:
            ops = svc.docker.ops(host)
        except Exception as err:  # noqa: BLE001 - pass the resolve error through
            return None, error_response(502, "resolve host client: " + str(err))
        return ops, None

    async def _maybe_pull(
        ops: Ops, spec: ContainerSpec, registry_id: str
    ) -> None:
        """Authenticated pull of ``spec.image`` when requested.

        Performs a pull when ``spec.pull_image`` is set or an explicit
        ``registry_id`` is given, resolving credentials from the stored
        registries. On success it disables ``spec.pull_image`` so the daemon
        does not re-pull anonymously during create.
        """
        if not spec.pull_image and registry_id == "":
            return
        auth = await registry.docker_auth_for(svc.store, spec.image, registry_id)
        rc = await ops.pull_image(spec.image, auth)
        # Drain and close the progress stream (the pull completes when it ends).
        try:
            async for _chunk in rc:  # type: ignore[union-attr]
                pass
        finally:
            close = getattr(rc, "close", None)
            if close is not None:
                res = close()
                if hasattr(res, "__await__"):
                    await res
        # The image is present; avoid an anonymous re-pull in create_container.
        spec.pull_image = False

    # -- read handlers (any authenticated role) -----------------------------

    async def list_containers(request: web.Request) -> web.StreamResponse:
        ops, err = _resolve_ops(request)
        if err is not None:
            return err
        all_ = _is_truthy(request.query.get("all"))
        try:
            containers = await ops.list_containers(all_)
        except Exception as ex:  # noqa: BLE001
            return ops_error_response(ex)
        return json_response(containers, status=200)

    async def inspect_container(request: web.Request) -> web.StreamResponse:
        ops, err = _resolve_ops(request)
        if err is not None:
            return err
        cid = request.match_info["id"]
        try:
            info = await ops.inspect_container(cid)
        except Exception as ex:  # noqa: BLE001
            return ops_error_response(ex)
        if not authz.host_allowed(request.match_info["hostID"], authz.LEVEL_OPERATE):
            info = redact_inspect(info)
        return json_response(info, status=200)

    async def container_spec(request: web.Request) -> web.StreamResponse:
        ops, err = _resolve_ops(request)
        if err is not None:
            return err
        cid = request.match_info["id"]
        try:
            spec = await ops.container_spec(cid)
        except Exception as ex:  # noqa: BLE001
            return ops_error_response(ex)
        if not authz.host_allowed(request.match_info["hostID"], authz.LEVEL_OPERATE):
            for kv in spec.env or []:
                kv.value = MASK
        return json_response(spec, status=200)

    # -- create (operator) --------------------------------------------------

    async def create_container(request: web.Request) -> web.StreamResponse:
        ops, err = _resolve_ops(request)
        if err is not None:
            return err

        try:
            body = await read_json(request)
        except web.HTTPException:
            return error_response(400, "invalid request body")
        if not isinstance(body, dict):
            return error_response(400, "invalid request body")

        try:
            spec = _spec_from_body(body)
        except Exception:  # noqa: BLE001 - malformed spec payload
            return error_response(400, "invalid request body")

        name = body.get("name", "") or ""
        registry_id = body.get("registryId", "") or ""

        if spec.image == "":
            return error_response(400, "image is required")
        port_err = _validate_ports(spec.ports)
        if port_err is not None:
            return error_response(400, port_err)

        try:
            await _maybe_pull(ops, spec, registry_id)
        except Exception as ex:  # noqa: BLE001
            return ops_error_response(ex)

        try:
            new_id = await ops.create_container(name, spec, True)
        except Exception as ex:  # noqa: BLE001
            status, msg = _classify_create_error(ex)
            return error_response(status, msg)
        return json_response({"id": new_id}, status=201)

    # -- lifecycle (operator) -----------------------------------------------

    def _lifecycle(op_name: str, status_word: str):
        """Build a lifecycle handler that calls ops.<op_name>(id).

        On success returns 200 {"status": status_word}; any error -> 502.
        Shared by start/stop/restart/pause/unpause.
        """

        async def handler(request: web.Request) -> web.StreamResponse:
            ops, err = _resolve_ops(request)
            if err is not None:
                return err
            cid = request.match_info["id"]
            try:
                await getattr(ops, op_name)(cid)
            except Exception as ex:  # noqa: BLE001
                return ops_error_response(ex)
            return json_response({"status": status_word}, status=200)

        return handler

    async def remove_container(request: web.Request) -> web.StreamResponse:
        ops, err = _resolve_ops(request)
        if err is not None:
            return err
        cid = request.match_info["id"]
        force = _is_truthy(request.query.get("force"))
        try:
            await ops.remove_container(cid, force)
        except Exception as ex:  # noqa: BLE001
            return ops_error_response(ex)
        return json_response({"status": "removed"}, status=200)

    # -- route table --------------------------------------------------------

    base = "/hosts/{hostID}"

    # Read routes: available to any authenticated role.
    api_route(app, "GET", base + "/containers", list_containers)
    api_route(app, "GET", base + "/containers/{id}", inspect_container)
    api_route(app, "GET", base + "/containers/{id}/spec", container_spec)

    # Mutating routes: operator (admins always allowed via require_role).
    api_route(
        app, "POST", base + "/containers", require_role(_OPERATOR)(create_container)
    )
    api_route(
        app,
        "POST",
        base + "/containers/{id}/start",
        require_role(_OPERATOR)(_lifecycle("start_container", "started")),
    )
    api_route(
        app,
        "POST",
        base + "/containers/{id}/stop",
        require_role(_OPERATOR)(_lifecycle("stop_container", "stopped")),
    )
    api_route(
        app,
        "POST",
        base + "/containers/{id}/restart",
        require_role(_OPERATOR)(_lifecycle("restart_container", "restarted")),
    )
    api_route(
        app,
        "POST",
        base + "/containers/{id}/pause",
        require_role(_OPERATOR)(_lifecycle("pause_container", "paused")),
    )
    api_route(
        app,
        "POST",
        base + "/containers/{id}/unpause",
        require_role(_OPERATOR)(_lifecycle("unpause_container", "unpaused")),
    )
    api_route(
        app,
        "DELETE",
        base + "/containers/{id}",
        require_role(_OPERATOR)(remove_container),
    )
