"""Export: render port's current state as ``kowan/v1`` YAML files.

The output is exactly what a sync reads back, so committing an export and
pointing a sync at it yields an all-``noop`` plan (round trip). Files are laid
out one resource per file under the kind's directory
(``deployments/web.yaml``, ``alert-rules/cpu.yaml``...).

Delivery: an in-memory zip (download) or a commit pushed to a branch of a
sync's repository using the sync's stored git credential.
"""

from __future__ import annotations

import asyncio
import io
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import _visible
from .kinds import Context, get_kind, kinds
from .resources import dump_yaml, render_document


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return s or "resource"


def list_exportable(ctx: Context) -> List[Dict[str, Any]]:
    managed = {(m["kind"], m["resource_id"]): m["sync_id"] for m in ctx.store.list_gitops_managed()}
    out = []
    for h in kinds():
        try:
            cur = h.current(ctx)
        except Exception:  # noqa: BLE001
            continue
        for c in cur.values():
            out.append({"kind": h.kind, "id": c.id, "name": c.name,
                        "managedBy": managed.get((h.kind, c.id), "")})
    return out


def export_files(
    ctx: Context, selection: Optional[Sequence[Tuple[str, str]]] = None
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Render the selected ``(kind, id)`` resources (all when None).

    Returns ``(files, warnings)`` with files as ``{path, kind, name, content}``.
    """
    wanted = set(selection) if selection is not None else None
    files: List[Dict[str, str]] = []
    warnings: List[str] = []
    used: set = set()
    for h in kinds():
        if wanted is not None and not any(k == h.kind for k, _ in wanted):
            continue
        for c in sorted(h.current(ctx).values(), key=lambda c: c.name):
            if wanted is not None and (h.kind, c.id) not in wanted:
                continue
            spec = h.export_spec(_visible(c.spec))
            if h.kind == "NotificationChannel":
                try:
                    from kowan.notify import provider as notify

                    sens = notify.sensitive_keys(c.spec.get("type", ""))
                    data = ctx.store.credential_config(c.id)
                    for k in sens:
                        if data.get(k) and k not in (c.spec.get("config") or {}):
                            warnings.append(f"channel {c.name!r}: sensitive field {k!r} omitted; "
                                            f"reference a Kowan secret ([[name]]) to manage it from git")
                except Exception:  # noqa: BLE001
                    pass
            path = posixpath.join(h.plural, _safe_name(c.name) + ".yaml")
            n = 2
            while path in used:
                path = posixpath.join(h.plural, f"{_safe_name(c.name)}-{n}.yaml")
                n += 1
            used.add(path)
            files.append({"path": path, "kind": h.kind, "name": c.name,
                          "content": dump_yaml(render_document(h.kind, c.name, spec))})
    return files, warnings


def zip_bytes(files: List[Dict[str, str]], prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(posixpath.join(prefix, f["path"]) if prefix else f["path"], f["content"])
    return buf.getvalue()


async def push_files(
    *, repo_url: str, base_ref: str, branch: str, path: str, files: List[Dict[str, str]],
    message: str, auth: Any = None, author: str = "kowan", email: str = "kowan@localhost",
) -> Dict[str, Any]:
    """Commit ``files`` under ``path`` on ``branch`` (created from
    ``base_ref`` when new) and push. Returns ``{branch, commit, changed}``."""
    from kowan.deployment import gitsource

    branch = branch.strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("-", "/")) or ".." in branch:
        raise ValueError("invalid branch name")
    rel = (path or "").strip().strip("/")
    if ".." in rel.split("/"):
        raise ValueError("path must stay inside the repository")
    tmp = tempfile.mkdtemp(prefix="kowan-gitops-export-")
    dest = os.path.join(tmp, "repo")
    try:
        # Prefer the target branch if it exists (append a commit), else base.
        try:
            await gitsource.ls_remote(repo_url, branch, auth)
            start = branch
        except gitsource.GitError:
            start = base_ref
        await gitsource.clone(repo_url, start, dest, auth)
        run = gitsource._run_git  # noqa: SLF001 - shared CLI wrapper (env-injected auth)
        if start != branch:
            await run(["checkout", "-q", "-B", branch], None, cwd=dest)
        root = os.path.join(dest, rel) if rel else dest
        for f in files:
            target = os.path.realpath(os.path.join(root, f["path"]))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                raise ValueError(f"export path {f['path']!r} escapes the repository")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(f["content"])
        await run(["add", "-A"], None, cwd=dest)
        status = await run(["status", "--porcelain"], None, cwd=dest)
        changed = bool(status.strip())
        if changed:
            await run(["-c", f"user.name={author}", "-c", f"user.email={email}",
                       "commit", "-q", "-m", message or "kowan: export resources"], None, cwd=dest)
        sha = (await run(["rev-parse", "HEAD"], None, cwd=dest)).strip()
        if changed or start != branch:
            await run(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], auth, cwd=dest)
        return {"branch": branch, "commit": sha, "changed": changed}
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
