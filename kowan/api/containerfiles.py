"""Container file browser: browse / view / edit / upload / download / mkdir /
rename / delete inside a container's filesystem.

Everything goes through the Docker Engine archive API (``HEAD|GET|PUT
/containers/{id}/archive``) via the ``Ops.container_*`` methods, so it works on
stopped containers, on images without a shell (distroless, scratch) and on
remote hosts (the same ops travel over the agent tunnel). The only things the
archive API cannot express are delete and rename: those run ``rm`` / ``mv``
through exec and therefore need a running container that ships the binaries.

Routes (under ``/api/v1/hosts/{hostID}/containers/{id}/files``):

  GET    ?path=              list a directory          any role
  GET    /stat?path=         stat one path             any role
  GET    /content?path=      read a text file (<=1 MiB) operator
  PUT    /content?path=      write a text file (raw body) operator
  GET    /download?path=     file bytes, or a .tar of a directory  operator
  POST   /upload?path=dir    multipart "file" (<=1 GiB) operator
  POST   /mkdir              {"path": dir, "name": n}  operator
  POST   /rename             {"path": p, "name": n}    operator
  DELETE ?path=              recursive delete          operator

Reading file bytes is operator-only, like the volume explorer: files often hold
secrets. Every write also records an explicit audit entry whose ``detail``
carries the container and path (the generic audit middleware only sees the
route, not the query string).
"""

from __future__ import annotations

import logging
import posixpath
import tempfile
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from aiohttp import web

from kowan import errors
from kowan.docker import containerfs as cfs
from kowan.docker.base import Ops
from kowan.store import NotFoundError
from kowan.web import (
    REQUEST_USER_KEY,
    api_route,
    bind_svc,
    client_ip,
    json_response,
    ops_error_response,
    read_body,
    read_json,
    require_role,
)

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services

log = logging.getLogger("kowan.api.containerfiles")

# Largest file the viewer/editor opens inline.
MAX_EDIT_BYTES = 1 << 20
# Largest single upload (same cap as the volume explorer).
MAX_UPLOAD_BYTES = 1 << 30
# Most entries returned for one directory listing.
MAX_LIST_ENTRIES = 2000

_CHUNK = 64 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_ops(svc: "Services", request: web.Request) -> Ops:
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


async def _audit(svc: "Services", request: web.Request, action: str, detail: str, status: int) -> None:
    rec = getattr(svc, "audit", None)
    if rec is None:
        return
    user = request.get(REQUEST_USER_KEY)
    entry = {
        "id": str(uuid.uuid4()),
        "action": action,
        "path": request.rel_url.path,
        "status": status,
        "ip": client_ip(request),
        "userId": getattr(user, "id", "") or "" if user is not None else "",
        "username": getattr(user, "username", "") or "" if user is not None else "",
        "detail": detail[:1000],
    }
    try:
        res = rec.record(entry)
        if res is not None and hasattr(res, "__await__"):
            await res
    except Exception as err:  # noqa: BLE001 - never fail the request on audit
        log.error("audit container files: %s", err)


def _not_found(exc: BaseException) -> bool:
    """True for a 404, from the local daemon or relayed over the agent tunnel."""
    return int(getattr(exc, "status", 0) or 0) == 404


def _cid(request: web.Request) -> str:
    cid = request.match_info.get("id", "")
    if not cid or "/" in cid:
        raise errors.Validation("invalid container id")
    return cid


def _attachment(name: str) -> str:
    safe = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{safe}"'


async def _close(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if close is not None:
        res = close()
        if hasattr(res, "__await__"):
            await res


async def resolve(ops: Ops, cid: str, path: str) -> Tuple[str, Dict[str, Any]]:
    """Stat ``path`` following symlinks (max 8 hops); returns (path, stat)."""
    cur = cfs.normalize_path(path)
    for _ in range(8):
        st = await ops.container_path_stat(cid, cur)
        if not st.get("isLink"):
            return cur, st
        cur = cfs.resolve_link(cur, st.get("linkTarget") or "")
    raise errors.Validation("too many levels of symbolic links")


async def _owner_of(ops: Ops, cid: str, path: str) -> Optional[cfs.TarHeader]:
    """First tar header of ``path`` (its owner/mode) without reading content."""
    try:
        stream = await ops.container_get_archive(cid, path)
    except Exception:  # noqa: BLE001
        return None
    try:
        return await cfs.first_header(stream)
    except Exception:  # noqa: BLE001
        return None
    finally:
        await _close(stream)


async def _ownership(ops: Ops, cid: str, target: str, *, is_new_dir: bool = False) -> Dict[str, Any]:
    """uid/gid/mode for writing ``target``: keep an existing file's, else inherit
    the parent directory's owner with default permissions."""
    existing = None
    try:
        st = await ops.container_path_stat(cid, target)
        if st.get("kind") == "file":
            existing = await _owner_of(ops, cid, target)
    except errors.AppError as exc:
        if not _not_found(exc):
            raise
        existing = None
    if existing is not None:
        return {"mode": existing.mode & 0o7777, "uid": existing.uid, "gid": existing.gid,
                "uname": existing.uname, "gname": existing.gname}
    par = await _owner_of(ops, cid, cfs.parent(target))
    mode = 0o755 if is_new_dir else 0o644
    if par is None:
        return {"mode": mode, "uid": 0, "gid": 0, "uname": "", "gname": ""}
    return {"mode": mode, "uid": par.uid, "gid": par.gid, "uname": par.uname, "gname": par.gname}


def decode_text(data: bytes) -> Tuple[str, bool]:
    """(text, is_binary): NUL bytes or invalid UTF-8 mark the payload binary."""
    if b"\x00" in data:
        return "", True
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._d = data

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._d)
        out, self._d = self._d[:n], self._d[n:]
        return out


async def _container_running(ops: Ops, cid: str) -> bool:
    info = await ops.inspect_container(cid)
    st = (info or {}).get("State") or {}
    return bool(st.get("Running")) and not bool(st.get("Paused"))


async def _exec_or_conflict(ops: Ops, cid: str, cmd: list, verb: str) -> None:
    if not await _container_running(ops, cid):
        raise errors.Conflict(f"{verb} needs a running container (the archive API cannot remove files); start it first")
    res = await ops.container_exec_status(cid, cmd, "0")
    code = int(res.get("exitCode", -1))
    if code == 0:
        return
    out = str(res.get("output", "")).strip()
    if code in (126, 127) or "executable file not found" in out:
        raise errors.Conflict(f"{verb} needs '{cmd[0]}' inside the container and this image does not ship it")
    raise errors.Conflict(f"{verb} failed: {out[-300:] or f'exit code {code}'}")


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def _list(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    try:
        path = cfs.normalize_path(request.query.get("path", "/"))
        out = await ops.container_list_dir(cid, path, MAX_LIST_ENTRIES)
        running = await _container_running(ops, cid)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    out = dict(out or {})
    out.setdefault("path", path)
    out["requested"] = path
    out["running"] = running
    out.setdefault("entries", [])
    return json_response(out)


async def _stat(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    try:
        st = await ops.container_path_stat(cid, cfs.normalize_path(request.query.get("path", "/")))
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(st)


async def _read(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    try:
        resolved, st = await resolve(ops, cid, request.query.get("path", ""))
        if st.get("kind") != "file":
            raise errors.Validation("not a regular file")
        size = int(st.get("size") or 0)
        if size > MAX_EDIT_BYTES:
            return json_response({"path": resolved, "size": size, "tooLarge": True,
                                  "limit": MAX_EDIT_BYTES, "binary": False, "content": ""})
        data = await ops.container_read_file(cid, resolved, MAX_EDIT_BYTES)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    text, binary = decode_text(data)
    return json_response({"path": resolved, "size": len(data), "tooLarge": False,
                          "limit": MAX_EDIT_BYTES, "binary": binary, "content": text,
                          "mode": st.get("mode", ""), "modTime": st.get("modTime", 0)})


async def _write(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    raw_path = request.query.get("path", "")
    status = 200
    detail = f"container={cid} path={raw_path}"
    try:
        path = cfs.normalize_path(raw_path)
        if path == "/":
            raise errors.Validation("not a file")
        body = await read_body(request, MAX_EDIT_BYTES + 1)
        if len(body) > MAX_EDIT_BYTES:
            raise errors.AppError(f"content exceeds the {MAX_EDIT_BYTES} byte edit limit", status=413)
        target = path
        try:
            resolved, st = await resolve(ops, cid, path)
            if st.get("isDir"):
                raise errors.Validation("path is a directory")
            target = resolved
        except errors.AppError as exc:
            if not _not_found(exc):
                raise
            pass
        detail = f"container={cid} path={target} bytes={len(body)}"
        own = await _ownership(ops, cid, target)
        tar = cfs.tar_file_bytes(posixpath.basename(target), body, **own)
        await ops.container_put_archive(cid, cfs.parent(target), _Bytes(tar))
    except Exception as err:  # noqa: BLE001
        resp = ops_error_response(err)
        await _audit(svc, request, "container.files.write", detail, resp.status)
        return resp
    await _audit(svc, request, "container.files.write", detail, status)
    return json_response({"status": "saved", "path": target, "size": len(body)})


async def _download(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    try:
        resolved, st = await resolve(ops, cid, request.query.get("path", "/"))
        stream = await ops.container_get_archive(cid, resolved)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    base = posixpath.basename(resolved) or "rootfs"
    resp = web.StreamResponse(status=200)
    if st.get("isDir"):
        resp.headers["Content-Type"] = "application/x-tar"
        resp.headers["Content-Disposition"] = _attachment(base + ".tar")
    else:
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Content-Disposition"] = _attachment(base)
        if st.get("size") is not None:
            resp.content_length = int(st.get("size") or 0)
    await resp.prepare(request)
    try:
        if st.get("isDir"):
            while True:
                chunk = await stream.read(_CHUNK)
                if not chunk:
                    break
                await resp.write(chunk)
        else:
            async for chunk in cfs.iter_first_file(stream):
                await resp.write(chunk)
    finally:
        await _close(stream)
    await resp.write_eof()
    return resp


class _SpoolReader:
    def __init__(self, f) -> None:
        self._f = f

    async def read(self, n: int = -1) -> bytes:
        return self._f.read(n if n and n > 0 else -1)


async def _upload(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    raw_dir = request.query.get("path", "/")
    detail = f"container={cid} dir={raw_dir}"
    try:
        dir_path, st = await resolve(ops, cid, raw_dir)
        if not st.get("isDir"):
            raise errors.Validation("upload target is not a directory")
        try:
            reader = await request.multipart()
        except Exception:  # noqa: BLE001
            raise errors.Validation("invalid upload")
        field = None
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                field = part
                break
        if field is None:
            raise errors.Validation("missing file field")
        name = cfs.validate_name(posixpath.basename((field.filename or "").replace("\\", "/")))
        target = cfs.join(dir_path, name)
        detail = f"container={cid} path={target}"
        spool = tempfile.SpooledTemporaryFile(max_size=8 << 20)
        try:
            size = 0
            while True:
                chunk = await field.read_chunk(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise errors.AppError("upload exceeds the 1 GiB limit", status=413)
                spool.write(chunk)
            spool.seek(0)
            detail = f"container={cid} path={target} bytes={size}"
            own = await _ownership(ops, cid, target)
            tar = cfs.TarFileStream(name, size, _SpoolReader(spool), **own)
            await ops.container_put_archive(cid, dir_path, tar)
        finally:
            spool.close()
    except Exception as err:  # noqa: BLE001
        resp = ops_error_response(err)
        await _audit(svc, request, "container.files.upload", detail, resp.status)
        return resp
    await _audit(svc, request, "container.files.upload", detail, 200)
    return json_response({"status": "uploaded", "path": target, "size": size})


async def _body(request: web.Request) -> Dict[str, Any]:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    return body


async def _mkdir(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    detail = f"container={cid}"
    try:
        body = await _body(request)
        dir_path, st = await resolve(ops, cid, body.get("path", "/"))
        if not st.get("isDir"):
            raise errors.Validation("parent is not a directory")
        name = cfs.validate_name(body.get("name"))
        target = cfs.join(dir_path, name)
        detail = f"container={cid} path={target}"
        try:
            await ops.container_path_stat(cid, target)
            raise errors.Conflict(f"{name} already exists")
        except errors.AppError as exc:
            if not _not_found(exc):
                raise
            pass
        own = await _ownership(ops, cid, target, is_new_dir=True)
        await ops.container_put_archive(cid, dir_path, _Bytes(cfs.tar_dir(name, **own)))
    except Exception as err:  # noqa: BLE001
        resp = ops_error_response(err)
        await _audit(svc, request, "container.files.mkdir", detail, resp.status)
        return resp
    await _audit(svc, request, "container.files.mkdir", detail, 201)
    return json_response({"status": "created", "path": target}, status=201)


async def _rename(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    detail = f"container={cid}"
    try:
        body = await _body(request)
        src = cfs.normalize_path(body.get("path", ""))
        if src == "/":
            raise errors.Validation("cannot rename the root directory")
        name = cfs.validate_name(body.get("name"))
        dst = cfs.join(cfs.parent(src), name)
        detail = f"container={cid} path={src} to={dst}"
        await ops.container_path_stat(cid, src)  # 404 if missing
        if dst != src:
            try:
                await ops.container_path_stat(cid, dst)
                raise errors.Conflict(f"{name} already exists")
            except errors.AppError as exc:
                if not _not_found(exc):
                    raise
                pass
            await _exec_or_conflict(ops, cid, ["mv", "--", src, dst], "rename")
    except Exception as err:  # noqa: BLE001
        resp = ops_error_response(err)
        await _audit(svc, request, "container.files.rename", detail, resp.status)
        return resp
    await _audit(svc, request, "container.files.rename", detail, 200)
    return json_response({"status": "renamed", "path": dst})


async def _delete(svc: "Services", request: web.Request) -> web.StreamResponse:
    ops = _resolve_ops(svc, request)
    cid = _cid(request)
    raw = request.query.get("path", "")
    detail = f"container={cid} path={raw}"
    try:
        path = cfs.normalize_path(raw)
        if path == "/":
            raise errors.Validation("refusing to delete the root directory")
        detail = f"container={cid} path={path}"
        await ops.container_path_stat(cid, path)  # 404 if missing
        await _exec_or_conflict(ops, cid, ["rm", "-rf", "--", path], "delete")
    except Exception as err:  # noqa: BLE001
        resp = ops_error_response(err)
        await _audit(svc, request, "container.files.delete", detail, resp.status)
        return resp
    await _audit(svc, request, "container.files.delete", detail, 200)
    return json_response({"status": "deleted", "path": path})


def register(app: web.Application, svc: "Services") -> None:
    h = bind_svc(svc)
    op = require_role("operator")
    base = "/hosts/{hostID}/containers/{id}/files"
    api_route(app, "GET", base, h(_list))
    api_route(app, "GET", base + "/stat", h(_stat))
    api_route(app, "GET", base + "/content", op(h(_read)))
    api_route(app, "PUT", base + "/content", op(h(_write)))
    api_route(app, "GET", base + "/download", op(h(_download)))
    api_route(app, "POST", base + "/upload", op(h(_upload)))
    api_route(app, "POST", base + "/mkdir", op(h(_mkdir)))
    api_route(app, "POST", base + "/rename", op(h(_rename)))
    api_route(app, "DELETE", base, op(h(_delete)))
