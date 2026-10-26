"""Deployment CRUD.

Provides :class:`_DeploymentMixin`, folded into :class:`kowan.store.store.Store`.
Targets (a string list) and Spec (a ContainerSpec) are stored as JSON columns:
targets is a JSON array TEXT (default '[]'), spec is nullable JSON TEXT (SQL NULL
when the deployment has no single-container spec, e.g. compose deployments).
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Optional

from kowan.models import ContainerSpec, Deployment, GitSource, PreviewInfo
from kowan.models.deployment import KeyValue

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_DEPLOYMENT_COLUMNS = (
    "id, name, type, strategy, targets, replicas, spec, compose, status, "
    "redeploy_token, protected, enabled, created_at, updated_at, "
    "git_source, deployed_commit, last_sync_at, preview, promote_to"
)

# Every read joins the stack variables, kept in a table of their own.
_SELECT = (
    f"SELECT {_DEPLOYMENT_COLUMNS}, variables FROM deployments "
    "LEFT JOIN deployment_variables ON deployment_id = id"
)


def _marshal_git(d: Deployment) -> Optional[str]:
    """Encode the optional GitSource as JSON TEXT (SQL NULL when absent)."""
    if d.git is None:
        return None
    return json.dumps(d.git.model_dump(by_alias=True), separators=(",", ":"))


def _marshal_preview(d: Deployment) -> Optional[str]:
    if d.preview is None:
        return None
    return json.dumps(d.preview.model_dump(by_alias=True), separators=(",", ":"))


def _marshal_deployment(d: Deployment) -> tuple[str, Optional[str]]:
    """Encode Targets and Spec to their JSON column representations.

    Returns (targets_json, spec_json_or_None). A None Spec is stored as SQL NULL.
    """
    targets = json.dumps(list(d.targets or []), separators=(",", ":"))
    if d.spec is None:
        return targets, None
    # camelCase JSON (the ContainerSpec field aliases), same shape as the API.
    spec = json.dumps(d.spec.model_dump(by_alias=True), separators=(",", ":"))
    return targets, spec


def _row_to_deployment(row: sqlite3.Row) -> Deployment:
    targets_raw = row["targets"]
    targets: list[str] = []
    if targets_raw:
        parsed = json.loads(targets_raw)
        if parsed:
            targets = [str(t) for t in parsed]

    spec: Optional[ContainerSpec] = None
    spec_raw = row["spec"]
    if spec_raw:
        spec = ContainerSpec.model_validate(json.loads(spec_raw))

    return Deployment(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        strategy=row["strategy"],
        targets=targets,
        replicas=row["replicas"],
        spec=spec,
        compose=row["compose"],
        status=row["status"],
        redeployToken=row["redeploy_token"] or "",
        protected=row["protected"] != 0,
        enabled=row["enabled"] != 0,
        git=GitSource.model_validate(json.loads(row["git_source"])) if row["git_source"] else None,
        deployedCommit=row["deployed_commit"] or "",
        lastSyncAt=row["last_sync_at"] or "",
        preview=PreviewInfo.model_validate(json.loads(row["preview"])) if row["preview"] else None,
        promoteTo=row["promote_to"] or "",
        variables=[KeyValue.model_validate(v) for v in json.loads(row["variables"] or "[]")],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    )


def _row_to_revision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "number": row["number"],
        "createdAt": row["created_at"],
        "author": row["author"],
        "source": row["source"],
        "note": row["note"],
        "config": json.loads(row["config"]),
        "deployedAt": row["deployed_at"],
    }


class _DeploymentMixin(_Base):
    def create_deployment(self, d: Deployment, *, author: str = "", source: str = "create") -> None:
        """Insert a new deployment, marshaling Targets and Spec to JSON columns.

        Records revision 1 (``author``/``source`` describe who and what created it).
        """
        targets, spec = _marshal_deployment(d)
        self._execute(
            f"INSERT INTO deployments ({_DEPLOYMENT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d.id, d.name, d.type, d.strategy, targets, d.replicas, spec,
                d.compose, d.status, d.redeploy_token, bool_to_int(d.protected),
                bool_to_int(d.enabled), d.created_at, d.updated_at,
                _marshal_git(d), d.deployed_commit, d.last_sync_at, _marshal_preview(d), d.promote_to,
            ),
        )
        self._save_variables(d)
        self.record_deployment_revision(d, author=author, source=source)

    def get_deployment(self, id: str) -> Deployment:
        """Return the deployment with the given id, raising NotFoundError if absent."""
        row = self._one(f"{_SELECT} WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get deployment: no rows in result set")
        return _row_to_deployment(row)

    def get_deployment_by_name(self, name: str) -> Deployment:
        """Return the deployment with the given (unique) name."""
        row = self._one(f"{_SELECT} WHERE name = ?", (name,))
        if row is None:
            raise NotFoundError("get deployment: no rows in result set")
        return _row_to_deployment(row)

    def get_deployment_by_redeploy_token(self, token: str) -> Deployment:
        """Return the deployment whose redeploy webhook token matches. An empty
        token never matches (a deployment without a hook stores '')."""
        if token == "":
            raise NotFoundError("get deployment: no rows in result set")
        row = self._one(
            f"{_SELECT} WHERE redeploy_token = ?", (token,)
        )
        if row is None:
            raise NotFoundError("get deployment: no rows in result set")
        return _row_to_deployment(row)

    def list_deployments(self) -> list[Deployment]:
        """Return all deployments ordered by creation time."""
        rows = self._query(f"{_SELECT} ORDER BY created_at")
        return [_row_to_deployment(r) for r in rows]

    def update_deployment(self, d: Deployment, *, author: str = "", source: str = "update", note: str = "") -> None:
        """Update the mutable fields of a deployment.

        When what it runs changed, a new revision is recorded; status, enabled
        and protection updates record nothing.
        """
        targets, spec = _marshal_deployment(d)
        self._execute(
            "UPDATE deployments SET name = ?, type = ?, strategy = ?, targets = ?, replicas = ?, "
            "spec = ?, compose = ?, status = ?, redeploy_token = ?, protected = ?, enabled = ?, "
            "updated_at = ?, git_source = ?, deployed_commit = ?, last_sync_at = ?, preview = ?, promote_to = ? "
            "WHERE id = ?",
            (
                d.name, d.type, d.strategy, targets, d.replicas, spec, d.compose,
                d.status, d.redeploy_token, bool_to_int(d.protected),
                bool_to_int(d.enabled), d.updated_at, _marshal_git(d),
                d.deployed_commit, d.last_sync_at, _marshal_preview(d), d.promote_to, d.id,
            ),
        )
        self._save_variables(d)
        self.record_deployment_revision(d, author=author, source=source, note=note)

    def _save_variables(self, d: Deployment) -> None:
        if not d.variables:
            self._execute("DELETE FROM deployment_variables WHERE deployment_id = ?", (d.id,))
            return
        self._execute(
            "INSERT INTO deployment_variables (deployment_id, variables) VALUES (?, ?) "
            "ON CONFLICT(deployment_id) DO UPDATE SET variables = excluded.variables",
            (d.id, json.dumps([v.model_dump() for v in d.variables], separators=(",", ":"))),
        )

    def delete_deployment(self, id: str) -> None:
        """Remove a deployment by id, with its revisions, its stack variables
        and the secret holding its secret environment values; deployments that
        promoted to it no longer do."""
        from kowan.deployment import envsecrets

        envsecrets.delete_for(self, id)
        self._execute("DELETE FROM deployments WHERE id = ?", (id,))
        self._execute("DELETE FROM deployment_variables WHERE deployment_id = ?", (id,))
        self._execute("UPDATE deployments SET promote_to = '' WHERE promote_to = ?", (id,))
        self._execute("DELETE FROM deployment_revisions WHERE deployment_id = ?", (id,))

    # -- revisions -----------------------------------------------------------

    def record_deployment_revision(
        self, d: Deployment, *, author: str = "", source: str = "", note: str = ""
    ) -> Optional[int]:
        """Store ``d``'s snapshot as the next revision unless it equals the
        latest one. Returns the new number, or None when nothing changed."""
        from kowan.deployment import revisions

        config = revisions.encode(revisions.snapshot(d))
        last = self._one(
            "SELECT number, config FROM deployment_revisions WHERE deployment_id = ? "
            "ORDER BY number DESC LIMIT 1",
            (d.id,),
        )
        if last is not None and last["config"] == config:
            return None
        number = (last["number"] if last is not None else 0) + 1
        self._execute(
            "INSERT INTO deployment_revisions (deployment_id, number, created_at, author, source, note, config) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d.id, number, d.updated_at or d.created_at, author, source, note, config),
        )
        self._execute(
            "DELETE FROM deployment_revisions WHERE deployment_id = ? AND number <= ?",
            (d.id, number - revisions.KEEP),
        )
        return number

    def list_deployment_revisions(self, deployment_id: str) -> list[dict[str, Any]]:
        """All kept revisions, newest first, with their decoded config."""
        rows = self._query(
            "SELECT number, created_at, author, source, note, config, deployed_at "
            "FROM deployment_revisions WHERE deployment_id = ? ORDER BY number DESC",
            (deployment_id,),
        )
        return [_row_to_revision(r) for r in rows]

    def get_deployment_revision(self, deployment_id: str, number: int) -> dict[str, Any]:
        row = self._one(
            "SELECT number, created_at, author, source, note, config, deployed_at "
            "FROM deployment_revisions WHERE deployment_id = ? AND number = ?",
            (deployment_id, number),
        )
        if row is None:
            raise NotFoundError("get deployment revision: no rows in result set")
        return _row_to_revision(row)

    def mark_deployment_revision_deployed(self, deployment_id: str, at: str) -> None:
        """Stamp the latest revision as the one now running."""
        self._execute(
            "UPDATE deployment_revisions SET deployed_at = ? WHERE deployment_id = ? AND number = "
            "(SELECT MAX(number) FROM deployment_revisions WHERE deployment_id = ?)",
            (at, deployment_id, deployment_id),
        )
