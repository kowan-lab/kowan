"""Docker image HTTP handlers, scoped per host.

List / inspect / history / remove images, pull (with a streamed NDJSON progress body) and build
(a build result carrying the combined logs). All routes live under
``/hosts/{hostID}`` and resolve the concrete :class:`kowan.docker.base.Ops` for
the addressed host via ``svc.docker.ops(host)``. Image prune is served by the
generic ``POST /hosts/{hostID}/prune/{kind}`` route in :mod:`kowan.api.system`.

Registry authentication goes through the one canonical resolver in
:mod:`kowan.registry` (``docker_auth_for`` / per-type ``Provider``):

  * For a **pull** the base64 ``X-Registry-Auth`` token comes from
    ``registry.docker_auth_for(store, ref, registryId)`` and is handed to
    ``ops.pull_image``.
  * For a **build** the registry's ``Provider.credentials()`` yields the
    effective ``(host, user, pass)`` — static for generic/ghcr, freshly
    exchanged for ecr/gcp/acr — stamped into ``BuildOptions`` so the build can
    ``docker login`` before pushing; ``kowan.docker`` only sees these fields.

A missing registry, or any provider/auth failure, degrades to anonymous access
(empty token) for pulls — so an image without stored credentials still pulls.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web

from kowan import registry
from kowan.web import (
    ops_error_response,
    SERVICES_KEY,
    api_route,
    error_response,
    json_response,
    read_json,
    require_role,
)

log = logging.getLogger("kowan.api.images")


# ---------------------------------------------------------------------------
# register(app, svc)
# ---------------------------------------------------------------------------


def register(app: web.Application, svc: Any) -> None:
    """Mount the image routes under ``/hosts/{hostID}``.

    Read routes (list/inspect) are viewer-accessible; the mutating routes (pull,
    remove, build) require at least the operator role.
    """
    base = "/hosts/{hostID}"

    api_route(app, "GET", base + "/images", _list_images)
    api_route(app, "GET", base + "/images/{id}/inspect", _inspect_image)

    api_route(app, "POST", base + "/images/pull", require_role("operator")(_pull_image))
    api_route(
        app, "DELETE", base + "/images/{id}", require_role("operator")(_remove_image)
    )
    api_route(app, "POST", base + "/build", require_role("operator")(_build_image))
    # Image prune is served by the generic POST /hosts/{hostID}/prune/{kind} in
    # system.py (kind="images", ?all=), so no dedicated route is mounted here.


# ---------------------------------------------------------------------------
# host / ops resolution
# ---------------------------------------------------------------------------


async def _resolve_ops(request: web.Request):
    """Resolve the ``Ops`` for the ``{hostID}`` URL param.

    On failure raises an aiohttp response (via ``error_response``) with the same
    status codes as the other per-host modules: 400 missing id, 404 host not found,
    502 client-resolution failure. Returns ``(svc, ops)`` on success.
    """
    svc = request.app[SERVICES_KEY]
    host_id = request.match_info.get("hostID", "")
    if host_id == "":
        raise _resp(error_response(400, "missing host id"))

    try:
        host = svc.store.get_host(host_id)
    except Exception:  # noqa: BLE001 - any lookup failure is "not found"
        raise _resp(error_response(404, "host not found"))

    try:
        ops = svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001
        raise _resp(error_response(502, "resolve host client: " + str(err)))
    return svc, ops


def _resp(response: web.Response) -> web.HTTPException:
    """Wrap a prepared Response in an HTTPException so handlers can ``raise`` it.

    ``error_response`` returns a ``web.Response`` (not an exception); the
    middleware chain renders raised ``HTTPException``s, so we adapt one into the
    other by copying its status/body, matching how the httpx JSONError path works.
    """
    # NOTE: assigning ``status_code`` on an already-constructed HTTPException
    # does not change the status it renders (aiohttp fixes it at __init__), so
    # the old version answered every unknown-host / unresolvable-host request
    # with an invalid ``HTTP/1.1 -1`` status line. JSONError sets the status
    # before construction.
    import json as _json

    from kowan.web.httpx import JSONError

    try:
        msg = _json.loads(response.text).get("error", "")
    except Exception:  # noqa: BLE001
        msg = response.text or ""
    return JSONError(response.status, msg)


def _is_truthy(value: Optional[str]) -> bool:
    """Report whether a query-string value should be treated as true."""
    if value is None:
        return False
    return value.lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Build push credentials — the pull path uses registry.docker_auth_for directly.
# ---------------------------------------------------------------------------


async def _fill_registry_credentials(svc: Any, opts: Any) -> None:
    """Stamp docker-login creds into ``opts`` from ``opts.registry_id``.

    Resolves the registry's auth through the CONNECTION model
    (``connections.resolve_service`` with ``dockerRegistryAuth``), so a registry
    that delegates its credentials to a shared connection is honored exactly like
    the pull path — no inline-only fallback. The resulting Provider yields its
    effective ``(user, password, host)`` — static for generic/ghcr, freshly
    exchanged for ecr/gcp/acr — which the build uses to ``docker login`` before
    pushing. A missing registry or any provider/auth failure raises ``Validation``
    so the operator gets a clear message instead of a silent anonymous push. Falls
    back to the registry credential's configured URL when the provider omits a
    server address.
    """
    from kowan.errors import Validation
    from kowan import capabilities as caps
    from kowan import connections

    try:
        provider = await connections.resolve_service(
            svc.store, opts.registry_id, caps.CAP_DOCKER_REGISTRY_AUTH
        )
    except Exception:  # noqa: BLE001
        raise Validation("registry not found")

    try:
        user, password, host = await provider.credentials()
    except Exception as err:  # noqa: BLE001
        raise Validation(f"resolve registry credentials: {err}")

    if host == "":
        try:
            host = svc.store.get_credential(opts.registry_id).url or ""
        except Exception:  # noqa: BLE001
            host = ""
    opts.registry_host = host
    opts.registry_user = user
    opts.registry_pass = password


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


async def _list_images(request: web.Request) -> web.StreamResponse:
    """GET /hosts/{hostID}/images — the images present on the host."""
    _svc, ops = await _resolve_ops(request)
    try:
        images = await ops.list_images()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(images)


async def _inspect_image(request: web.Request) -> web.StreamResponse:
    """GET /hosts/{hostID}/images/{id}/inspect — inspect JSON + build history.

    History is best-effort: a failure still returns the inspect data. Response
    shape: ``{"inspect": ..., "history": ...}``.
    """
    _svc, ops = await _resolve_ops(request)
    image_id = request.match_info["id"]
    try:
        inspect = await ops.inspect_image(image_id)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)

    history: Any = []
    try:
        history = await ops.image_history(image_id)
    except Exception:  # noqa: BLE001 - best-effort
        history = []

    return json_response({"inspect": inspect, "history": history})


async def _remove_image(request: web.Request) -> web.StreamResponse:
    """DELETE /hosts/{hostID}/images/{id}?force= — remove an image."""
    _svc, ops = await _resolve_ops(request)
    image_id = request.match_info["id"]
    force = _is_truthy(request.query.get("force"))
    try:
        await ops.remove_image(image_id, force)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "removed"})


async def _pull_image(request: web.Request) -> web.StreamResponse:
    """POST /hosts/{hostID}/images/pull with body {ref, registryId?}.

    The Docker pull progress stream is forwarded to the client as a chunked
    ``application/x-ndjson`` response (one JSON line per progress event) so the
    caller can follow progress in real time (each line is flushed as it
    arrives). ``registryId`` may also be supplied as
    a query parameter for simple clients.
    """
    _svc, ops = await _resolve_ops(request)
    svc = request.app[SERVICES_KEY]

    try:
        req = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(req, dict):
        return error_response(400, "invalid request body")

    ref = str(req.get("ref", "") or "")
    registry_id = str(req.get("registryId", "") or "")
    if registry_id == "":
        registry_id = request.query.get("registryId", "") or ""
    if ref == "":
        return error_response(400, "ref is required")

    auth = await registry.docker_auth_for(svc.store, ref, registry_id)
    try:
        stream = await ops.pull_image(ref, auth)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)

    response = web.StreamResponse(
        status=200, headers={"Content-Type": "application/x-ndjson"}
    )
    await response.prepare(request)
    try:
        # ops.pull_image returns an AsyncByteStream (already NDJSON from the
        # Engine). Forward it chunk-by-chunk; each chunk is flushed to the client.
        async for chunk in stream:
            if not chunk:
                continue
            await response.write(chunk)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
    await response.write_eof()
    return response


async def _build_image(request: web.Request) -> web.StreamResponse:
    """POST /hosts/{hostID}/build with a BuildOptions body.

    Builds (and optionally pushes) an image from an inline Dockerfile or a Git
    repository. When ``registryId`` is set the stored registry's docker-login
    credentials are resolved into ``opts.registry_host/user/pass``.

    Returns HTTP 200 even on a failed build/push: the ``BuildResult`` carries
    ``success=false`` plus the combined output so the UI can show the logs. A
    raised error from ``build_image`` means the build could not be attempted and
    maps to a 502.
    """
    from kowan.docker.base import BuildOptions

    _svc, ops = await _resolve_ops(request)
    svc = request.app[SERVICES_KEY]

    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    try:
        opts = BuildOptions.model_validate(body)
    except Exception:  # noqa: BLE001 - pydantic validation failure
        return error_response(400, "invalid request body")

    if opts.tag == "":
        return error_response(400, "tag is required")
    if opts.git_url == "" and opts.dockerfile == "":
        return error_response(400, "a gitUrl or an inline dockerfile is required")

    if opts.registry_id != "":
        try:
            await _fill_registry_credentials(svc, opts)
        except Exception as err:  # noqa: BLE001 - Validation carries the message
            return error_response(400, str(err))

    try:
        result = await ops.build_image(opts)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    # 200 even on a failed build/push: BuildResult.success=false + output.
    return json_response(result)
