"""Who may manage workflows with server-side steps, and where a backup lands.

A script step runs bash on the control plane and a backup step writes an archive
of the whole control-plane state to its disk, so only an admin may create,
change, run or expose (webhook) a workflow that contains one. REST and MCP both
call this, so the rule cannot drift between them.

A backup step always writes under ``{data_dir}/backups``; its ``path`` only
names a sub-folder there, so it can never write elsewhere on the server.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from kowan.models import WorkflowStepBackup, WorkflowStepScript

ADMIN_ONLY = "workflows with script or backup steps can only be managed by an admin"

# The step types only an admin may manage.
ADMIN_STEP_TYPES = (WorkflowStepScript, WorkflowStepBackup)

# The folder under the data directory that backup steps write into.
BACKUP_DIR = "backups"


def has_admin_step(steps: Optional[Iterable]) -> bool:
    return any(getattr(st, "type", "") in ADMIN_STEP_TYPES for st in (steps or []))


def step_rights_problem(role: str, *step_lists) -> str:
    """The reason ``role`` may not act on these steps, or "" when it may."""
    if role == "admin" or not any(has_admin_step(steps) for steps in step_lists):
        return ""
    return ADMIN_ONLY


def backup_path_problem(path: str) -> str:
    """Why ``path`` is not a valid backup sub-folder, or "" when it is."""
    p = (path or "").replace("\\", "/")
    if p.startswith("/") or os.path.isabs(path or ""):
        return "backup path must be a folder inside <data>/backups, not an absolute path"
    if any(part == ".." for part in p.split("/")):
        return "backup path must stay inside <data>/backups"
    return ""


def backup_dir(data_dir: str, path: str) -> str:
    """The directory a backup step writes into: ``{data_dir}/backups[/path]``.

    Raises :class:`ValueError` for a path that would leave it, symlinks included.
    """
    problem = backup_path_problem(path)
    if problem:
        raise ValueError(problem)
    base = os.path.join(data_dir, BACKUP_DIR)
    dest = os.path.join(base, *[part for part in (path or "").replace("\\", "/").split("/") if part])
    real_base, real_dest = os.path.realpath(base), os.path.realpath(dest)
    if real_dest != real_base and not real_dest.startswith(real_base + os.sep):
        raise ValueError("backup path must stay inside <data>/backups")
    return dest
