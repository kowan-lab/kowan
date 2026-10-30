"""Host management REST API.

CRUD for Docker hosts (the local daemon
and remote agents), a live connectivity probe (``ping``) that augments each
returned host with its current online status, and the edge-enrollment command
helpers. ``ensure_local_host(svc)`` creates the fixed ``"local"`` host row on
startup.

Wire it up from the server bootstrap with ``register(app, svc)`` and call
``ensure_local_host(svc)`` once before serving.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from aiohttp import web

from kowan.auth import require_role
from kowan.auth import authorize as authz
from kowan.errors import BadGateway, Conflict, NotFound, Validation
from kowan.models import Host
from kowan.models.host import (
    HostModeAgent,
    HostModeEdge,
    HostTypeAgent,
    HostTypeLocal,
)
from kowan.web import (
    SERVICES_KEY,
    api_route,
    json_response,
    now_rfc3339,
    read_json,
    reject_unknown_fields,
)

log = logging.getLogger("kowan.api.hosts")

# LocalHostID is the fixed identifier used for the local Docker daemon host row.
LocalHostID = "local"

# Longest host name accepted; names show in the host picker and in tables.
MAX_NAME_LENGTH = 64

# Bounds each per-host liveness probe so a single unreachable agent
# cannot stall the whole list response.
_PING_TIMEOUT = 3.0


def register(app: web.Application, svc) -> None:
    """Mount the host management routes under ``/api/v1``.

    Read routes are open to any authenticated principal (viewer+); mutating
    routes require ``admin``.
    """
    api_route(app, "GET", "/hosts", _list_hosts)
    api_route(app, "POST", "/hosts", require_role("admin")(_create_host))
    api_route(app, "GET", "/hosts/{hostID}", _get_host)
    api_route(app, "GET", "/hosts/{hostID}/swarm", _swarm)
    api_route(app, "PATCH", "/hosts/{hostID}", require_role("admin")(_rename_host))
    api_route(app, "DELETE", "/hosts/{hostID}", require_role("admin")(_delete_host))
    api_route(
        app,
        "GET",
        "/hosts/{hostID}/edge-command",
        require_role("admin")(_edge_command),
    )
    # Agent self-update + rollback (docs/updates-design.md): status/history for
    # operators, update/rollback for admins (they replace the agent container).
    api_route(app, "GET", "/hosts/{hostID}/agent/update",
              require_role("operator")(_agent_update_check))
    api_route(app, "POST", "/hosts/{hostID}/agent/update",
              require_role("admin")(_agent_update))
    api_route(app, "POST", "/hosts/{hostID}/agent/rollback",
              require_role("admin")(_agent_rollback))
    api_route(app, "GET", "/hosts/{hostID}/agent/updates",
              require_role("operator")(_agent_update_history))


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


# --- liveness -------------------------------------------------------------


async def _ping(svc, h: Host) -> bool:
    """Report whether the host's docker Ops answers Ping within a short timeout.

    Any error (no provider, unresolvable Ops, or failed/timed-out ping) yields
    ``False`` rather than propagating.
    """
    provider = getattr(svc, "docker", None)
    if provider is None:
        return False

    try:
        ops = provider.ops(h)
    except Exception:  # noqa: BLE001 - unresolvable Ops => offline
        return False

    try:
        await asyncio.wait_for(ops.ping(), timeout=_PING_TIMEOUT)
    except asyncio.TimeoutError:
        log.debug("host ping timed out: host=%s", h.id)
        return False
    except Exception as err:  # noqa: BLE001
        # A real error (not a plain timeout) means the tunnel is up but the op
        # failed — e.g. the agent can't reach the Docker socket. Log it at INFO
        # so an "offline" host that IS connected is diagnosable, not silent.
        log.info("host ping failed: host=%s error=%s", h.id, err)
        return False
    return True


# --- read handlers --------------------------------------------------------


async def _list_hosts(request: web.Request) -> web.Response:
    """Return all hosts, each augmented with a live ``online`` status."""
    svc = _svc(request)
    hosts = authz.filter_hosts(svc.store.list_hosts())
    # Ping every host concurrently: sequentially, each offline host added up to
    # _PING_TIMEOUT to the response time of the (frequently polled) host list.
    results = await asyncio.gather(*(_ping(svc, h) for h in hosts))
    for h, online in zip(hosts, results):
        h.online = online
    return json_response(hosts)


async def _get_host(request: web.Request) -> web.Response:
    """Return a single host with a live ``online`` status."""
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    h = _load_host(svc, host_id)
    h.online = await _ping(svc, h)
    return json_response(h)


async def _swarm(request: web.Request) -> web.Response:
    """Report the swarm topology for a host: ``{"active": bool, "nodes": [...]}``.

    ``active`` is whether the host is an active swarm manager; ``nodes`` lists the
    swarm's nodes (empty when the host is not a manager). Read-only, so any
    authenticated principal may call it (like the container/volume list routes).
    """
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    h = _load_host(svc, host_id)
    try:
        ops = svc.docker.ops(h)
    except Exception as err:  # noqa: BLE001 - unresolvable Ops => upstream failure
        raise BadGateway("resolve host client: " + str(err))
    active = await ops.swarm_active()
    nodes = await ops.list_swarm_nodes() if active else []
    return json_response({"active": active, "nodes": nodes})


# --- agent self-update -----------------------------------------------------
# Thin handlers over kowan.updater (shared helpers in kowan.api.serverupdate).


async def _agent_update_check(request: web.Request) -> web.Response:
    """Agent update status: versions, digests, previous, in-progress and last run.
    Best-effort: problems are reported in ``reason``/``error``, never as 5xx."""
    from kowan.api.serverupdate import manager, refresh_param
    from kowan.updater import COMPONENT_AGENT

    svc = _svc(request)
    h = _load_host(svc, request.match_info["hostID"])
    return json_response(await manager(svc).status(COMPONENT_AGENT, h, refresh=refresh_param(request)))


async def _agent_start(request: web.Request, mode: str, allowed: set) -> web.Response:
    from kowan.api.serverupdate import image_field, manager, optional_body, raise_update_error, username
    from kowan.updater import COMPONENT_AGENT
    from kowan.updater.orchestrator import UpdateError

    svc = _svc(request)
    h = _load_host(svc, request.match_info["hostID"])
    body = await optional_body(request, allowed)
    try:
        uid = await manager(svc).start(COMPONENT_AGENT, h, mode=mode, image=image_field(body),
                                       user=username(request))
    except UpdateError as err:
        raise_update_error(err)
    return json_response({"id": uid}, status=202)


async def _agent_update(request: web.Request) -> web.Response:
    """Start a transactional agent update (body ``{image?}``) -> 202 ``{id}``."""
    return await _agent_start(request, "update", {"image"})


async def _agent_rollback(request: web.Request) -> web.Response:
    """Swap the agent back to ``kowan-agent-previous`` -> 202 ``{id}``."""
    return await _agent_start(request, "rollback", set())


async def _agent_update_history(request: web.Request) -> web.Response:
    from kowan.api.serverupdate import limit_param
    from kowan.updater import COMPONENT_AGENT

    svc = _svc(request)
    h = _load_host(svc, request.match_info["hostID"])
    return json_response(svc.store.list_updates(COMPONENT_AGENT, h.id, limit_param(request)))


# --- create ---------------------------------------------------------------


async def _create_host(request: web.Request) -> web.Response:
    """Create a new host and return it with the docker command to join the agent.

    The creation flow is deliberately minimal: a name, the mode, and — for
    ``agent`` mode — the address the server will dial. A per-host token is
    ALWAYS generated server-side (never supplied by the client), so the same
    docker command carries it in both directions:

    - ``edge`` (agent->server): the agent dials ``/edge/connect`` and presents
      the token; no address is needed.
    - ``agent`` (server->agent): the agent LISTENS and the server dials
      ``address``; the token is verified by the agent. NOTE: the server-side
      dialer for this mode is not wired yet, so an agent host is created but
      stays offline until that transport lands.
    """
    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    # Only name/mode/address are accepted now — the token is server-generated.
    reject_unknown_fields(body, {"name", "mode", "address"})

    name = _check_name(svc, body.get("name"))

    mode = body.get("mode") or HostModeAgent
    if mode not in (HostModeAgent, HostModeEdge):
        raise Validation('mode must be "agent" or "edge"')

    h = Host(
        id=str(uuid.uuid4()),
        name=name,
        type=HostTypeAgent,
        mode=mode,
        online=False,
        lastSeen="",
        createdAt=now_rfc3339(),
        # Strong per-host token, generated for BOTH modes (the agent sends it in
        # edge mode, verifies it in agent mode).
        token=uuid.uuid4().hex + uuid.uuid4().hex,
    )

    if mode == HostModeEdge:
        h.address = ""
    else:
        address = body.get("address") or ""
        if address == "":
            raise Validation("address is required")
        h.address = address

    svc.store.create_host(h)

    # Report current liveness on creation for convenience.
    h.online = await _ping(svc, h)

    return json_response(
        {"host": h, "dockerRun": _docker_run(svc, request, h)},
        status=201,
    )


# --- edge command ---------------------------------------------------------


async def _edge_command(request: web.Request) -> web.Response:
    """Re-display the docker join command for an existing host (admin).

    Works for both modes (agent + edge). The fixed local host has no agent to
    join, so it is refused.
    """
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    h = _load_host(svc, host_id)
    if h.id == LocalHostID or h.type == HostTypeLocal:
        raise Validation("the local host has no agent to join")

    h.online = await _ping(svc, h)
    return json_response({"host": h, "dockerRun": _docker_run(svc, request, h)})


# --- delete ---------------------------------------------------------------


def _check_name(svc, raw, host_id: str = "") -> str:
    """Validate a host name for create (``host_id`` empty) or rename.

    GitOps files refer to a host by id or by name, so a name may not repeat
    another host's name or id (compared case-insensitively).
    """
    if raw is not None and not isinstance(raw, str):
        raise Validation("name must be a string")
    name = (raw or "").strip()
    if name == "":
        raise Validation("name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise Validation(f"name must be at most {MAX_NAME_LENGTH} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise Validation("name must not contain control characters")
    key = name.casefold()
    for other in svc.store.list_hosts():
        if other.id == host_id:
            continue
        if key in (other.name.casefold(), other.id.casefold()):
            raise Conflict(f'another host is already called "{name}"')
    return name


async def _rename_host(request: web.Request) -> web.Response:
    """Rename a host, the local one included. Only the display name changes:
    the id, and everything keyed on it (deployments, permissions, history),
    stays the same."""
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    h = _load_host(svc, host_id)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"name"})
    h.name = _check_name(svc, body.get("name"), h.id)
    svc.store.rename_host(h.id, h.name)
    h.online = await _ping(svc, h)
    return json_response(h)


async def _delete_host(request: web.Request) -> web.Response:
    """Remove a host. The fixed local host cannot be deleted."""
    svc = _svc(request)
    host_id = request.match_info["hostID"]

    if host_id == LocalHostID:
        raise Validation("cannot delete the local host")

    # 404 if absent.
    _load_host(svc, host_id)

    svc.store.delete_host(host_id)
    svc.docker.forget(host_id)
    return json_response({"status": "deleted"})


# --- enrollment command helpers -------------------------------------------


def _origin(svc, request: web.Request) -> str:
    """Externally-reachable control-plane base URL for enrollment commands.

    Prefers ``config.external_url`` so commands point at a host-reachable
    address; otherwise derives scheme://host from the inbound request.
    """
    external = getattr(getattr(svc, "config", None), "external_url", "") or ""
    if external != "":
        return external.rstrip("/")
    return _request_origin(request)


def _request_origin(request: web.Request) -> str:
    """Derive scheme://host from the inbound request (honors X-Forwarded-Proto)."""
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    host = request.host
    return f"{scheme}://{host}"


def _agent_image() -> str:
    """The agent image name used in join commands.

    Override with ``KOWAN_AGENT_IMAGE`` (e.g. a registry-qualified tag like
    ``ghcr.io/you/kowan-agent:latest``) so the command works on hosts that must
    pull the image. Defaults to ``ghcr.io/kowan-lab/kowan-agent``.
    """
    return os.environ.get("KOWAN_AGENT_IMAGE", "").strip() or "ghcr.io/kowan-lab/kowan-agent"


def _docker_run(svc, request: web.Request, host: Host) -> str:
    """The ``docker run`` command to start the agent for ``host``.

    - edge: the agent dials OUT to this control plane (KOWAN_SERVER_URL); no
      inbound port is needed.
    - agent: the agent LISTENS on 9001 and the control plane dials it, so the
      port is published and no server URL is needed.

    Both restart the agent with Docker (``unless-stopped``), so it comes back
    after a crash or a host reboot.
    """
    image = _agent_image()
    base = (
        "docker run -d --name kowan-agent --restart unless-stopped "
        "-v /var/run/docker.sock:/var/run/docker.sock "
    )
    if host.mode == HostModeEdge:
        origin = _origin(svc, request)
        return (
            base
            + f"-e KOWAN_AGENT_MODE=edge -e KOWAN_SERVER_URL={origin} "
            + f"-e KOWAN_AGENT_TOKEN={host.token} {image}"
        )
    # agent mode: the server dials host.address; the agent just listens on 9001.
    return (
        base
        + "-p 9001:9001 "
        + "-e KOWAN_AGENT_MODE=agent "
        + f"-e KOWAN_AGENT_TOKEN={host.token} {image}"
    )


# --- store helpers --------------------------------------------------------


def _load_host(svc, host_id: str) -> Host:
    """Fetch a host or raise :class:`NotFound` (=> 404 "host not found")."""
    try:
        h = svc.store.get_host(host_id)
    except Exception as err:  # store raises store.NotFoundError
        raise NotFound("host not found") from err
    authz.check_host(h.id)  # team/grant scoping (kowan/auth/authorize.py)
    return h


# --- startup --------------------------------------------------------------


def ensure_local_host(svc) -> None:
    """Create the fixed ``"local"`` host row if it does not already exist.

    Safe to call on every startup. The
    Python store owns no ``ensure_local_host``, so we perform the get-or-create
    here against ``svc.store``.
    """
    try:
        svc.store.get_host(LocalHostID)
        return
    except Exception:  # noqa: BLE001 - store.NotFoundError => create it
        pass

    now = now_rfc3339()
    h = Host(
        id=LocalHostID,
        name="local",
        type=HostTypeLocal,
        mode=HostModeAgent,
        address="",
        token="",
        online=True,
        lastSeen=now,
        createdAt=now,
    )
    svc.store.create_host(h)
