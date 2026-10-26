"""Docker volume HTTP handlers, scoped per host.

Includes the browse/download/upload helpers (the concrete Ops implementations
own the volume-browsing primitives). Every route lives under
``/hosts/{hostID}/volumes``.

Browsing/reading a volume mounts it into a throwaway helper container, so those
routes are gated like a mutation (operator role) and expose raw file bytes only
to operators.
"""

from __future__ import annotations

import logging
import os
import posixpath
from typing import TYPE_CHECKING

from aiohttp import web

from kowan import errors
from kowan import volumeauth
from kowan.auth.middleware import user_from
from kowan.docker.base import Ops
from kowan.models import VolumeCreateOptions
from kowan.store import NotFoundError
from kowan.web import (
    ops_error_response,
    api_route,
    bind_svc,
    error_response,
    json_response,
    read_json,
    require_role,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.api.volumes")


# Bounds a single file upload into a volume (1 GiB).
MAX_VOLUME_UPLOAD_BYTES = 1 << 30

# Chunk size used when streaming download bodies out to the client.
_STREAM_CHUNK = 64 * 1024


async def _resolve_ops(svc: "Services", request: web.Request) -> Ops:
    """Resolve the :class:`Ops` for the ``{hostID}`` URL param (see networks.py)."""
    host_id = request.match_info.get("hostID", "")
    if not host_id:
        raise errors.Validation("missing host id")
    try:
        host = svc.store.get_host(host_id)
    except NotFoundError:
        raise errors.NotFound("host not found")
    try:
        return svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001
        raise errors.BadGateway("resolve host client: " + str(err))


def _download_name(sub: str, fallback: str) -> str:
    """Derive a safe download file name from a volume sub-path.

    Falls back to ``fallback`` when the path is empty or the volume root;
    otherwise the base of the cleaned, slash-trimmed path.
    """
    base = posixpath.basename("/" + sub.strip().strip("/"))
    if base in (".", "/", ""):
        return fallback
    return base


def _attachment(name: str) -> str:
    """Build a Content-Disposition header value, quoting the file name."""
    # Quote the file name, escaping backslashes and double quotes.
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{escaped}"'


async def _stream_body(request: web.Request, stream, content_type: str, disposition: str | None):
    """Stream an Ops byte stream out to the client as an attachment.

    Iterates the ``AsyncByteStream`` (aiohttp response body / in-memory wrapper)
    and always closes it when done.
    """
    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = content_type
    if disposition is not None:
        resp.headers["Content-Disposition"] = disposition
    await resp.prepare(request)
    try:
        async for chunk in stream:
            if chunk:
                await resp.write(chunk)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    await resp.write_eof()
    return resp


async def _list_volumes(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    try:
        volumes = await ops.list_volumes()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(volumes)


async def _create_volume(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    # Optional: delegate provider auth (SMB password, S3 keys, …) to a shared
    # connection. Resolved HERE on the control plane so the secret is injected into
    # the DriverOpts before the create RPC and never reaches the browser.
    conn_ref = str(body.get("connectionRef", "") or "")
    provider = str(body.get("provider", "") or "")
    try:
        req = VolumeCreateOptions.model_validate(body)  # pydantic errors are ValueErrors
    except ValueError:
        return error_response(400, "invalid request body")
    if not req.name:
        return error_response(400, "name is required")
    if _binds_host_path(req) and not _is_admin(request):
        # A local volume that is a bind of a host directory, browsed through the
        # volume explorer, is read/write access to the host's filesystem.
        return error_response(403, "volumes bound to a host directory can only be created by an admin")
    if conn_ref:
        try:
            req.options = _resolve_volume_connection(svc, provider, conn_ref, req.options)
        except volumeauth.VolumeAuthError as err:
            return error_response(400, str(err))
        except NotFoundError:
            return error_response(400, "referenced connection not found")
        except Exception as err:  # noqa: BLE001
            log.error("resolve volume connection: %s", err)
            return error_response(500, "failed to resolve volume connection")
    try:
        vol = await ops.create_volume(req)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(vol, status=201)


def _binds_host_path(req: VolumeCreateOptions) -> bool:
    """A ``local``-driver volume configured as a bind mount of a host path."""
    if (req.driver or "local") != "local":
        return False
    opts = {str(k).lower(): str(v) for k, v in (req.options or {}).items()}
    mount_opts = {o.strip() for o in opts.get("o", "").split(",")}
    device = opts.get("device", "")
    # "//server/share" is a CIFS/SMB network share, not a host path.
    return "bind" in mount_opts or (device.startswith("/") and not device.startswith("//"))


def _is_admin(request: web.Request) -> bool:
    user = user_from(request)
    return user is not None and user.role == "admin"


def _resolve_volume_connection(
    svc: "Services", provider: str, conn_ref: str, options: dict
) -> dict:
    """Load the referenced connection and inject its secret fields into ``options``
    per ``provider``. The secret is the connection's non-secret identity (meta,
    minus the reference itself) overlaid with its decrypted data."""
    cred = svc.store.get_credential(conn_ref)
    data = svc.store.credential_config(conn_ref)
    secret = {
        **{str(k): str(v) for k, v in cred.meta.items() if k != "connectionRef"},
        **{str(k): str(v) for k, v in data.items()},
    }
    return volumeauth.apply_connection(provider, cred.type, secret, options)


async def _inspect_volume(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    try:
        out = await ops.inspect_volume(request.match_info["name"])
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(out)


async def _browse_volume(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    sub = request.query.get("path", "")
    try:
        out = await ops.browse_volume(request.match_info["name"], sub)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    if out is None:
        out = []
    return json_response(out)


async def _read_volume_file(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = await _resolve_ops(svc, request)
    sub = request.query.get("path", "")
    try:
        data = await ops.read_volume_file(request.match_info["name"], sub)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return web.Response(body=data, content_type="application/octet-stream")


async def _download_volume_file(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = await _resolve_ops(svc, request)
    sub = request.query.get("path", "")
    try:
        stream = await ops.download_volume_file(request.match_info["name"], sub)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return await _stream_body(
        request,
        stream,
        "application/octet-stream",
        _attachment(_download_name(sub, "file")),
    )


async def _download_volume_archive(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = await _resolve_ops(svc, request)
    name = request.match_info["name"]
    sub = request.query.get("path", "")
    try:
        stream = await ops.download_volume_archive(name, sub)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return await _stream_body(
        request,
        stream,
        "application/zip",
        _attachment(_download_name(sub, name) + ".zip"),
    )


async def _upload_volume_file(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = await _resolve_ops(svc, request)
    dest_dir = request.query.get("path", "")

    # Read the multipart body, pulling the single "file" field. aiohttp streams
    # parts, so we buffer to a spooled temp file to learn the size before handing
    # a byte stream to Ops.upload_volume_file (which needs the size up front).
    # The upload cap is enforced while reading.
    try:
        reader = await request.multipart()
    except Exception:  # noqa: BLE001
        return error_response(400, "invalid upload")

    field = None
    filename = ""
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            field = part
            filename = part.filename or ""
            break

    if field is None:
        return error_response(400, "missing file field")

    import tempfile

    spool = tempfile.SpooledTemporaryFile(max_size=8 << 20)
    size = 0
    try:
        while True:
            chunk = await field.read_chunk(_STREAM_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_VOLUME_UPLOAD_BYTES:
                spool.close()
                return error_response(400, "invalid upload: request body too large")
            spool.write(chunk)
        spool.seek(0)

        class _FileStream:
            """Adapt the spooled temp file to an async byte stream for Ops."""

            def __init__(self, f):
                self._f = f

            async def read(self, n: int = -1) -> bytes:
                return self._f.read(n if n and n > 0 else -1)

            def __aiter__(self):
                return self

            async def __anext__(self) -> bytes:
                b = self._f.read(_STREAM_CHUNK)
                if not b:
                    raise StopAsyncIteration
                return b

        try:
            await ops.upload_volume_file(
                request.match_info["name"], dest_dir, filename, size, _FileStream(spool)
            )
        except Exception as err:  # noqa: BLE001
            return ops_error_response(err)
    finally:
        spool.close()

    return json_response({"status": "uploaded", "name": os.path.basename(filename)})


async def _remove_volume(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    name = request.match_info["name"]
    force = _is_truthy(request.query.get("force", ""))
    try:
        await ops.remove_volume(name, force)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "removed"})


def _is_truthy(v: str) -> bool:
    """Report whether a query value should be treated as true (1/true/yes/on)."""
    return v.strip().lower() in ("1", "true", "yes", "on")


def register(app: web.Application, svc: "Services") -> None:
    """Mount the per-host volume routes under ``/api/v1``.

    List / inspect are open to any authenticated principal; create, browse,
    read, download (file + archive), upload and remove require the operator role.
    """
    h = bind_svc(svc)
    base = "/hosts/{hostID}/volumes"

    api_route(app, "GET", base, h(_list_volumes))
    api_route(app, "GET", base + "/{name}/inspect", h(_inspect_volume))

    op = require_role("operator")
    api_route(app, "POST", base, op(h(_create_volume)))
    api_route(app, "GET", base + "/{name}/browse", op(h(_browse_volume)))
    api_route(app, "GET", base + "/{name}/file", op(h(_read_volume_file)))
    api_route(app, "GET", base + "/{name}/download", op(h(_download_volume_file)))
    api_route(app, "GET", base + "/{name}/archive", op(h(_download_volume_archive)))
    api_route(app, "POST", base + "/{name}/upload", op(h(_upload_volume_file)))
    api_route(app, "DELETE", base + "/{name}", op(h(_remove_volume)))
