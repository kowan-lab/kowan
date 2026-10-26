"""Server database snapshot / restore / retention.

* :func:`take_snapshot` copies the LIVE database with SQLite's online backup API
  (consistent while the server keeps writing, WAL included) into
  ``<data>/backups/pre-update-<id>.db``, verifies it with ``PRAGMA quick_check``
  and copies the key files next to it.
* :func:`restore_snapshot` runs in the helper while the server container is
  STOPPED. It never deletes the database it replaces: the current
  ``kowan.db`` (+ ``-wal``/``-shm``) is moved into ``backups/rolled-back-<id>/``
  first, so even a wrong restore is recoverable by hand. The snapshot is checked
  before anything is moved; a failure half-way puts the moved files back.
* :func:`prune_snapshots` keeps the newest N snapshot sets (and never the ones
  explicitly protected, e.g. the snapshot a manual rollback would need).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

BACKUPS_DIR = "backups"
KEY_FILES = ("secret.key", "jwt.key")
SNAPSHOT_PREFIX = "pre-update-"
ASIDE_PREFIX = "rolled-back-"
PREFLIGHT_PREFIX = "preflight-"
# Tables describing the INSTALLATION rather than the application data: they are
# copied from the database being replaced into the restored one, so a restore
# never rewinds the update history itself (otherwise the record of the very
# update/rollback being performed would vanish with the restore).
CARRY_TABLES = ("updates",)


class SnapshotError(RuntimeError):
    pass


def backups_dir(data_dir: str) -> Path:
    return Path(data_dir) / BACKUPS_DIR


def snapshot_path(data_dir: str, update_id: str) -> Path:
    return backups_dir(data_dir) / f"{SNAPSHOT_PREFIX}{update_id}.db"


def _key_copy(snapshot: Path, key: str) -> Path:
    return snapshot.with_name(snapshot.name[: -len(".db")] + "." + key)


def quick_check(path: str) -> None:
    """Raise SnapshotError unless ``path`` is a readable, intact SQLite db."""
    if not os.path.isfile(path):
        raise SnapshotError(f"snapshot {path} not found")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as err:
        raise SnapshotError(f"snapshot {path} unreadable: {err}") from err
    if not row or str(row[0]).lower() != "ok":
        raise SnapshotError(f"snapshot {path} failed integrity check: {row[0] if row else '?'}")


def take_snapshot(db_path: str, data_dir: str, update_id: str,
                  keys: Iterable[str] = KEY_FILES) -> str:
    """Snapshot ``db_path`` (and key files) for ``update_id``. Returns the path."""
    dest = snapshot_path(data_dir, update_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        src = sqlite3.connect(db_path, timeout=10)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                # The copy inherits WAL mode from the live db; make it a single
                # self-contained file (no -wal/-shm siblings to carry around).
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        quick_check(str(tmp))
        os.replace(tmp, dest)
    except (sqlite3.Error, OSError) as err:
        for leftover in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise SnapshotError(f"snapshot {db_path}: {err}") from err
    for key in keys:
        kp = Path(data_dir) / key
        if kp.is_file():
            shutil.copy2(kp, _key_copy(dest, key))
    return str(dest)


def copy_for_preflight(snapshot: str, data_dir: str, update_id: str) -> str:
    """A throwaway copy for the preflight container (never the snapshot itself)."""
    dest = backups_dir(data_dir) / f"{PREFLIGHT_PREFIX}{update_id}.db"
    shutil.copy2(snapshot, dest)
    return str(dest)


def restore_snapshot(db_path: str, snapshot: str, aside_dir: str,
                     keys: Iterable[str] = KEY_FILES) -> Dict[str, List[str]]:
    """Replace ``db_path`` with ``snapshot``; move the current files aside first.

    Returns ``{"moved": [...], "keys": [...]}``. Raises SnapshotError, in which
    case the live database is left (or put back) as it was.
    """
    quick_check(snapshot)
    db = Path(db_path)
    aside = Path(aside_dir)
    aside.mkdir(parents=True, exist_ok=True)
    moved: List[tuple] = []
    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(str(db) + suffix)
            if p.exists():
                target = aside / p.name
                os.replace(p, target)
                moved.append((p, target))
        tmp = Path(str(db) + ".restore-tmp")
        shutil.copyfile(snapshot, tmp)
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, db)
    except OSError as err:
        # Put back whatever we moved, so the installation is unchanged.
        for orig, target in reversed(moved):
            try:
                if not orig.exists():
                    os.replace(target, orig)
            except OSError:
                pass
        raise SnapshotError(f"restore {snapshot} -> {db_path}: {err}") from err
    carried = carry_tables(str(db), str(aside / db.name), CARRY_TABLES)
    restored_keys: List[str] = []
    snap = Path(snapshot)
    for key in keys:
        kc = _key_copy(snap, key)
        live = db.parent / key
        if kc.is_file():
            try:
                if live.is_file():
                    if live.read_bytes() == kc.read_bytes():
                        continue
                    shutil.copy2(live, aside / key)
                shutil.copy2(kc, live)
                restored_keys.append(key)
            except OSError as err:
                raise SnapshotError(f"restore key {key}: {err}") from err
    return {"moved": [str(t) for _, t in moved], "keys": restored_keys, "carried": carried}


def carry_tables(dst_path: str, src_path: str, tables: Iterable[str]) -> List[str]:
    """Upsert ``tables`` rows from ``src_path`` into ``dst_path``. Best-effort:
    returns the tables carried, never raises (a failure only loses history)."""
    done: List[str] = []
    if not os.path.isfile(src_path):
        return done
    try:
        conn = sqlite3.connect(dst_path, timeout=10)
    except sqlite3.Error:
        return done
    try:
        conn.execute("ATTACH DATABASE ? AS old", (src_path,))
        for t in tables:
            try:
                row = conn.execute("SELECT sql FROM old.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
                if row is None:
                    continue
                if conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is None:
                    conn.execute(row[0])
                cols_main = [r[1] for r in conn.execute(f'PRAGMA main.table_info("{t}")')]
                cols_old = {r[1] for r in conn.execute(f'PRAGMA old.table_info("{t}")')}
                cols = [c for c in cols_main if c in cols_old]
                if not cols:
                    continue
                collist = ", ".join(f'"{c}"' for c in cols)
                conn.execute(f'INSERT OR REPLACE INTO main."{t}" ({collist}) SELECT {collist} FROM old."{t}"')
                done.append(t)
            except sqlite3.Error:
                continue
        conn.commit()
        conn.execute("DETACH DATABASE old")
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return done


def prune_snapshots(data_dir: str, keep: int = 3, protect: Iterable[str] = (),
                    now: Optional[float] = None) -> List[str]:
    """Delete all but the newest ``keep`` snapshot sets (never ``protect`` ids).

    Also prunes ``rolled-back-*`` directories beyond ``keep`` and stale
    ``preflight-*`` copies older than an hour. Returns the removed paths.
    """
    bdir = backups_dir(data_dir)
    if not bdir.is_dir():
        return []
    protected = {str(p) for p in protect if p}
    removed: List[str] = []
    snaps = sorted(
        (p for p in bdir.glob(f"{SNAPSHOT_PREFIX}*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in snaps[keep:]:
        uid = p.name[len(SNAPSHOT_PREFIX):-len(".db")]
        if uid in protected:
            continue
        for f in [p] + [_key_copy(p, k) for k in KEY_FILES]:
            try:
                if f.exists():
                    f.unlink()
                    removed.append(str(f))
            except OSError:
                pass
    asides = sorted(
        (p for p in bdir.glob(f"{ASIDE_PREFIX}*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in asides[keep:]:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(str(p))
    cutoff = (now if now is not None else time.time()) - 3600
    for p in bdir.glob(f"{PREFLIGHT_PREFIX}*.db"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(str(p))
        except OSError:
            pass
    return removed
