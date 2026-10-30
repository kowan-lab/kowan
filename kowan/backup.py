"""Portable backup/restore of the Kowan control plane's persistent state.

A backup archives the SQLite database and the on-disk stack/deployment data
directory into a gzip-compressed tar containing:

    manifest.json   metadata identifying the archive as a kowan-backup
    kowan.db         a consistent SQLite snapshot (via store.snapshot)
    secret.key      the master encryption key, if held in a file (see below)
    jwt.key         the JWT signing key, if held in a file
    stacks/...      the {data_dir}/stacks subtree, if it exists

The encryption key (secret.key) and JWT key (jwt.key) ARE included when they
exist as files, so a restore recovers EVERYTHING from the backup alone —
encrypted secrets stay decryptable and sessions stay valid, with nothing to
carry over manually. SECURITY: this makes the backup itself as sensitive as the
master key — anyone with the archive can decrypt all stored secrets, so store
and transmit backups accordingly. (When a key is supplied via env instead of a
file — KOWAN_SECRET_KEY / KOWAN_JWT_SECRET — it has no file to archive; you manage
that key yourself and must configure the restore target with the same value.)

Restore is staged rather than applied in place: :func:`stage_restore` extracts the
archive into ``{data_dir}/.restore-pending`` and drops a ``RESTORE`` marker;
:func:`apply_pending_restore` (called at startup, BEFORE the database is opened)
swaps the staged files into place. This avoids hot-swapping a database file the
running server has open.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import shutil
import tarfile
from typing import IO, Protocol, Tuple

log = logging.getLogger("kowan.backup")

# --- archive entry names / manifest constants -------------------------------

# The archive entry identifying a Kowan backup.
MANIFEST_NAME = "manifest.json"
# The archive entry holding the SQLite snapshot.
DB_NAME = "kowan.db"
# The archive entries holding the master keys (only present when kept as files,
# not supplied via env). These MUST match the on-disk filenames in data_dir:
# secrets.cipher.KEY_FILE_NAME and config.JWT_KEY_FILE_NAME respectively.
SECRET_KEY_NAME = "secret.key"
JWT_KEY_NAME = "jwt.key"
# On-disk subdirectory (under data_dir) holding stack/deployment working data.
STACKS_DIR = "stacks"
# Marks an archive as a Kowan backup; stage_restore rejects a mismatching kind.
MANIFEST_KIND = "kowan-backup"
# Current backup format version.
MANIFEST_VERSION = 1

# Staging directory under data_dir into which an uploaded backup is extracted,
# awaiting application on the next startup.
PENDING_DIR_NAME = ".restore-pending"
# File whose presence in the staging dir signals a staged restore is ready.
RESTORE_MARKER = "RESTORE"

# 8 GiB cap on a single extracted file, guarding against a decompression bomb.
_MAX_FILE_SIZE = 8 << 30


class Snapshotter(Protocol):
    """The subset of :class:`kowan.store.Store` that :func:`create_backup` needs."""

    def snapshot(self, dest_path: str) -> None: ...


def _manifest_bytes() -> bytes:
    """Encode the archive manifest.

    ``createdAt`` is left empty (neither this writer nor the API handler stamps
    it).
    """
    manifest = {"version": MANIFEST_VERSION, "createdAt": "", "kind": MANIFEST_KIND}
    return json.dumps(manifest).encode("utf-8")


def create_backup(st: Snapshotter, data_dir: str, w: IO[bytes]) -> None:
    """Write a gzip+tar backup of the control-plane state to ``w``.

    Takes a consistent SQLite snapshot (via ``st.snapshot``) into a temporary
    file, then streams a manifest, the snapshot as ``kowan.db``, the master keys
    held as files in ``data_dir`` (``secret.key``, ``jwt.key``) and the
    ``stacks/`` subtree of ``data_dir`` (if present) into the archive.

    ``w`` is any binary file-like object with ``write`` (e.g. a StreamResponse
    adapter). The caller owns closing ``w``.
    """
    # Take the SQLite snapshot into a fresh temp path. VACUUM INTO refuses to
    # overwrite an existing file, so pick a name and ensure it does not exist,
    # letting SQLite create it.
    import tempfile

    fd, snap_path = tempfile.mkstemp(prefix="kowan-snapshot-", suffix=".db")
    os.close(fd)
    os.remove(snap_path)
    try:
        st.snapshot(snap_path)

        gz = gzip.GzipFile(fileobj=w, mode="wb")
        try:
            tw = tarfile.open(fileobj=gz, mode="w")
            try:
                # manifest.json first so readers can validate cheaply.
                _add_tar_bytes(tw, MANIFEST_NAME, _manifest_bytes())
                # kowan.db: the SQLite snapshot.
                _add_tar_file(tw, DB_NAME, snap_path)
                # The master keys, if held as files, so a restore recovers a
                # fully-working server (decryptable secrets + valid sessions).
                for key_name in (SECRET_KEY_NAME, JWT_KEY_NAME):
                    key_path = os.path.join(data_dir, key_name)
                    if os.path.isfile(key_path):
                        _add_tar_file(tw, key_name, key_path)
                # stacks/: the deployment/stack data subtree, if present.
                stacks_path = os.path.join(data_dir, STACKS_DIR)
                if os.path.isdir(stacks_path):
                    _add_tar_tree(tw, stacks_path, STACKS_DIR)
            finally:
                tw.close()
        finally:
            gz.close()
    finally:
        for p in (snap_path, snap_path + "-wal", snap_path + "-shm"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass


def _add_tar_bytes(tw: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add an in-memory blob as a regular file entry."""
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o600
    info.type = tarfile.REGTYPE
    tw.addfile(info, io.BytesIO(data))


def _add_tar_file(tw: tarfile.TarFile, name: str, src_path: str) -> None:
    """Copy an on-disk file into the archive under ``name``."""
    size = os.path.getsize(src_path)
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o600
    info.type = tarfile.REGTYPE
    with open(src_path, "rb") as f:
        tw.addfile(info, f)


def _add_tar_tree(tw: tarfile.TarFile, src_dir: str, prefix: str) -> None:
    """Walk ``src_dir`` and add each regular file under ``prefix`` (forward slashes),
    preserving structure. Symlinks and other non-regular files are skipped."""
    # Directory entry for the root prefix first, then its contents.
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        base = prefix if rel_root == "." else prefix + "/" + rel_root.replace(os.sep, "/")
        # Directory header.
        dinfo = tarfile.TarInfo(name=base + "/")
        dinfo.type = tarfile.DIRTYPE
        dinfo.mode = 0o700
        tw.addfile(dinfo)
        for fname in files:
            fpath = os.path.join(root, fname)
            if not os.path.isfile(fpath) or os.path.islink(fpath):
                # Skip symlinks, sockets, devices, etc.
                continue
            entry = base + "/" + fname
            _add_tar_file(tw, entry, fpath)


def _sanitize_entry(name: str) -> Tuple[str, bool, bool]:
    """Validate/normalize a tar entry name.

    Returns ``(clean, allowed, is_dir)``: the cleaned slash-form name, whether it
    is within the allow-list (manifest.json, kowan.db, or anything under stacks/),
    and whether it denotes a directory. Raises :class:`ValueError` on path
    traversal or absolute paths.
    """
    n = name.replace("\\", "/")
    if n.startswith("./"):
        n = n[2:]
    is_dir = n.endswith("/")
    n = n.strip("/")
    if n == "":
        return "", False, is_dir

    if name.startswith("/") or os.path.isabs(name):
        raise ValueError(f"invalid backup: absolute path entry {name!r}")
    for part in n.split("/"):
        if part == "..":
            raise ValueError(f"invalid backup: path traversal in entry {name!r}")

    if n == MANIFEST_NAME:
        return n, True, False
    if n == DB_NAME:
        return n, True, False
    if n == SECRET_KEY_NAME or n == JWT_KEY_NAME:
        return n, True, False
    if n == STACKS_DIR or n.startswith(STACKS_DIR + "/"):
        return n, True, is_dir
    return n, False, is_dir


def _write_regular_file(dest: str, src: IO[bytes]) -> None:
    """Create ``dest`` and copy ``src`` into it, capping the size."""
    with open(dest, "wb") as out:
        remaining = _MAX_FILE_SIZE
        while remaining > 0:
            chunk = src.read(min(1 << 20, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)


def stage_restore(data_dir: str, r: IO[bytes]) -> None:
    """Read a gzip+tar backup from ``r``, validate it, and stage it.

    Extracts the archive into ``{data_dir}/.restore-pending``; on success writes a
    ``RESTORE`` marker so :func:`apply_pending_restore` (run at the next startup)
    swaps the staged files into place. The archive must contain a valid
    ``manifest.json`` (kind ``kowan-backup``) and a ``kowan.db`` entry. Entry names
    are sanitized to defeat path traversal.

    Raises :class:`ValueError` on a malformed or invalid archive.
    """
    final_dir = os.path.join(data_dir, PENDING_DIR_NAME)
    # Extract next to it and swap in only once the archive validated, so a bad
    # upload never destroys a restore that is already staged.
    pending_dir = final_dir + ".incoming"
    shutil.rmtree(pending_dir, ignore_errors=True)
    os.makedirs(pending_dir, mode=0o700, exist_ok=True)

    saw_manifest = False
    saw_db = False

    try:
        gz = gzip.GzipFile(fileobj=r, mode="rb")
        # streaming tar read
        tr = tarfile.open(fileobj=gz, mode="r|")
        try:
            for member in tr:
                clean, allowed, is_dir = _sanitize_entry(member.name)
                if not allowed:
                    # Unknown top-level entry: ignore (forward compatibility).
                    continue

                if clean == MANIFEST_NAME:
                    f = tr.extractfile(member)
                    data = f.read(1 << 20) if f is not None else b""
                    try:
                        m = json.loads(data.decode("utf-8"))
                    except Exception as err:  # noqa: BLE001
                        raise ValueError(f"parse manifest: {err}") from err
                    if m.get("kind") != MANIFEST_KIND:
                        raise ValueError(
                            f"invalid backup: manifest kind {m.get('kind')!r}, "
                            f"want {MANIFEST_KIND!r}"
                        )
                    saw_manifest = True
                elif is_dir or member.isdir():
                    dest = os.path.join(pending_dir, clean.replace("/", os.sep))
                    os.makedirs(dest, mode=0o700, exist_ok=True)
                elif not member.isfile():
                    # Symlinks, devices, fifos: never restored (a streamed tar
                    # cannot even open them as files).
                    continue
                else:
                    dest = os.path.join(pending_dir, clean.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
                    f = tr.extractfile(member)
                    if f is None:  # pragma: no cover - isfile() above
                        continue
                    _write_regular_file(dest, f)
                    if clean == DB_NAME:
                        saw_db = True
        finally:
            tr.close()
    except ValueError:
        shutil.rmtree(pending_dir, ignore_errors=True)
        raise
    except Exception as err:  # noqa: BLE001 - malformed gzip/tar
        shutil.rmtree(pending_dir, ignore_errors=True)
        raise ValueError(f"read backup archive: {err}") from err

    if not saw_manifest:
        shutil.rmtree(pending_dir, ignore_errors=True)
        raise ValueError("invalid backup: missing or invalid manifest.json")
    if not saw_db:
        shutil.rmtree(pending_dir, ignore_errors=True)
        raise ValueError(f"invalid backup: missing {DB_NAME}")

    # Drop the marker last, so a crash mid-extraction never leaves a
    # half-extracted-but-armed staging dir.
    with open(os.path.join(pending_dir, RESTORE_MARKER), "wb") as mf:
        mf.write(b"1")
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(pending_dir, final_dir)


def apply_pending_restore(data_dir: str) -> bool:
    """Apply a previously staged restore, if one is armed.

    Checks for ``{data_dir}/.restore-pending/RESTORE``; when present, replaces
    ``kowan.db`` with the staged snapshot and ``stacks`` with the staged stacks
    directory, then removes the staging directory. Returns whether a restore was
    applied.

    MUST be called at startup BEFORE the store opens the database, so the file
    swap never races an open handle.
    """
    pending_dir = os.path.join(data_dir, PENDING_DIR_NAME)
    marker_path = os.path.join(pending_dir, RESTORE_MARKER)

    if not os.path.exists(marker_path):
        return False

    staged_db = os.path.join(pending_dir, DB_NAME)
    if os.path.exists(staged_db):
        # Replace the live database with the staged snapshot. Remove WAL/SHM
        # sidecars from the old database so a stale WAL cannot resurrect old data.
        live_db = os.path.join(data_dir, DB_NAME)
        for sidecar in (live_db + "-wal", live_db + "-shm"):
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass
        _replace_path(staged_db, live_db)
    else:
        # The marker is present but the staged DB is gone: a previous boot crashed
        # AFTER swapping the DB in but BEFORE removing the staging dir. The live DB
        # is already the restored one, so DON'T brick startup by raising — just
        # finish applying any still-staged keys/stacks below and clean up.
        log.warning(
            "restore: staged database already applied on a prior boot; "
            "finishing cleanup instead of failing startup"
        )

    # Restore the master keys if the backup carried them, so the restored
    # database's encrypted secrets decrypt and existing sessions stay valid.
    # Re-tighten to 0600 (the extractor may not preserve mode).
    for key_name in (SECRET_KEY_NAME, JWT_KEY_NAME):
        staged_key = os.path.join(pending_dir, key_name)
        if os.path.isfile(staged_key):
            live_key = os.path.join(data_dir, key_name)
            _replace_path(staged_key, live_key)
            try:
                os.chmod(live_key, 0o600)
            except OSError:
                pass

    # Replace the stacks directory if the backup carried one. If the staged
    # archive had no stacks/, leave the existing one untouched.
    staged_stacks = os.path.join(pending_dir, STACKS_DIR)
    if os.path.isdir(staged_stacks):
        live_stacks = os.path.join(data_dir, STACKS_DIR)
        shutil.rmtree(live_stacks, ignore_errors=True)
        _replace_path(staged_stacks, live_stacks)

    shutil.rmtree(pending_dir, ignore_errors=True)
    return True


def _replace_path(src: str, dst: str) -> None:
    """Move ``src`` to ``dst``, falling back to a copy on cross-device rename."""
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    elif os.path.exists(dst):
        try:
            os.remove(dst)
        except FileNotFoundError:
            pass
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass
    # Cross-device or other rename failure: fall back to a recursive copy.
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


__all__ = [
    "MANIFEST_NAME",
    "DB_NAME",
    "SECRET_KEY_NAME",
    "JWT_KEY_NAME",
    "STACKS_DIR",
    "MANIFEST_KIND",
    "MANIFEST_VERSION",
    "PENDING_DIR_NAME",
    "RESTORE_MARKER",
    "Snapshotter",
    "create_backup",
    "stage_restore",
    "apply_pending_restore",
]
