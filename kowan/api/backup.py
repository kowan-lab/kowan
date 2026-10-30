"""Admin-only backup/restore endpoints over the protected ``/api/v1`` surface.

- GET  /backup   streams a gzip+tar snapshot of the control-plane state (SQLite
  database + stacks data dir) as a file download.
- POST /restore  accepts an uploaded backup archive and stages it; the staged
  restore is applied on the next server start (see
  :func:`kowan.backup.apply_pending_restore`), avoiding a hot-swap of the open
  database.

Both routes are admin-only: a backup exposes the entire database and a restore
replaces it. The archive also carries the master keys kept as files
(``secret.key``, ``jwt.key``; see :mod:`kowan.backup`), so a restore recovers
decryptable secrets and valid sessions from the archive alone, and the archive
is as sensitive as those keys. A key supplied through the environment
(``KOWAN_SECRET_KEY`` / ``KOWAN_JWT_SECRET``) has no file and is not in the
archive: the restore target needs the same value.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from kowan import backup as backuplib
from kowan.web import api_route, bind_svc, error_response, json_response, require_role

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.api.backup")

# Chunk size for streaming the backup file to the client.
_CHUNK = 1 << 20


async def _download_backup(svc: "Services", request: web.Request) -> web.StreamResponse:
    """Stream a gzip+tar backup of the control-plane state as a file download.

    ``create_backup`` writes synchronously to a file-like object; to avoid
    blocking the event loop on the SQLite snapshot / large writes we build the
    archive into a temp file first, then stream it to the client in chunks.
    """
    filename = "kowan-backup-" + datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".tar.gz"
    data_dir = svc.config.data_dir

    fd, tmp_path = tempfile.mkstemp(prefix="kowan-backup-", suffix=".tar.gz")
    try:
        # Build the archive off the event loop; snapshot + tar can be heavy.
        def build() -> None:
            with os.fdopen(fd, "wb") as f:
                backuplib.create_backup(svc.store, data_dir, f)

        import asyncio

        try:
            await asyncio.to_thread(build)
        except Exception as err:  # noqa: BLE001
            # Nothing has been written to the response yet, so a JSON error is
            # still safe here (the snapshot is built before streaming starts).
            log.error("create backup: %s", err)
            return error_response(500, "failed to create backup")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/gzip",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
        await resp.prepare(request)
        with open(tmp_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


async def _upload_restore(svc: "Services", request: web.Request) -> web.Response:
    """Accept a backup archive (multipart ``file`` field, or the raw body) and
    stage it for application on the next server start.

    The archive bytes are spooled to a temp file, then handed to the synchronous
    :func:`kowan.backup.stage_restore` off the event loop.
    """
    data_dir = svc.config.data_dir
    content_type = request.headers.get("Content-Type", "")

    fd, tmp_path = tempfile.mkstemp(prefix="kowan-restore-", suffix=".tar.gz")
    try:
        with os.fdopen(fd, "wb") as f:
            if content_type.startswith("multipart/"):
                reader = await request.multipart()
                part = None
                async for p in reader:
                    if p.name == "file":
                        part = p
                        break
                if part is None:
                    return error_response(400, "missing file field")
                while True:
                    chunk = await part.read_chunk(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
            else:
                async for chunk in request.content.iter_chunked(_CHUNK):
                    f.write(chunk)

        import asyncio

        def stage() -> None:
            with open(tmp_path, "rb") as src:
                backuplib.stage_restore(data_dir, src)

        try:
            await asyncio.to_thread(stage)
        except ValueError as err:
            log.error("stage restore: %s", err)
            return error_response(400, "invalid backup archive")
        except Exception as err:  # noqa: BLE001
            log.error("stage restore: %s", err)
            return error_response(400, "invalid backup archive")
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass

    return json_response({"status": "restore staged; restart the server to apply"})


def register(app: web.Application, svc: "Services") -> None:
    """Mount the backup routes under ``/api/v1``. Both routes are admin-only."""
    h = bind_svc(svc)
    admin = require_role("admin")
    api_route(app, "GET", "/backup", admin(h(_download_backup)))
    api_route(app, "POST", "/restore", admin(h(_upload_restore)))
