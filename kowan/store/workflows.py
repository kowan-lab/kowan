"""Workflow + workflow-run CRUD.

Provides :class:`_WorkflowMixin`, folded into :class:`kowan.store.store.Store`.
Steps/Triggers (workflows) and Steps (runs) are stored as JSON blob columns.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from kowan.models import (
    StepResult,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowRunRunning,
    WorkflowTrigger,
)

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


# Canonical column lists for workflow / workflow_run selects.
_WORKFLOW_COLUMNS = (
    "id, name, description, steps, triggers, variables, enabled, run_token, "
    "protected, created_at, updated_at"
)
_RUN_COLUMNS = "id, workflow_id, trigger, status, steps, started_at, finished_at"


def _marshal_models(items) -> str:
    """JSON-encode a list of pydantic models (empty list when None)."""
    if items is None:
        items = []
    return json.dumps(
        [m.model_dump(by_alias=True) for m in items], separators=(",", ":")
    )


def _unmarshal(blob, model_cls) -> list:
    """Decode a JSON blob column into a list of the given pydantic model."""
    if not blob:
        return []
    raw = json.loads(blob)
    return [model_cls.model_validate(item) for item in raw]


def _marshal_map(m) -> str:
    """JSON-encode a plain string->string map (empty object when None)."""
    return json.dumps(m or {}, separators=(",", ":"))


def _unmarshal_map(blob) -> dict:
    """Decode a JSON blob column into a plain dict (empty when NULL/blank/bad)."""
    if not blob:
        return {}
    raw = json.loads(blob)
    return raw if isinstance(raw, dict) else {}


def _row_to_workflow(row: sqlite3.Row) -> Workflow:
    return Workflow(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        steps=_unmarshal(row["steps"], WorkflowStep),
        triggers=_unmarshal(row["triggers"], WorkflowTrigger),
        variables=_unmarshal_map(row["variables"]),
        enabled=row["enabled"] != 0,
        runToken=row["run_token"],
        protected=row["protected"] != 0,
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    )


def _row_to_run(row: sqlite3.Row) -> WorkflowRun:
    return WorkflowRun(
        id=row["id"],
        workflowId=row["workflow_id"],
        trigger=row["trigger"],
        status=row["status"],
        steps=_unmarshal(row["steps"], StepResult),
        startedAt=row["started_at"],
        finishedAt=row["finished_at"],
    )


class _WorkflowMixin(_Base):
    def create_workflow(self, w: Workflow) -> None:
        """Insert a new workflow, marshaling Steps/Triggers to JSON."""
        self._execute(
            f"INSERT INTO workflows ({_WORKFLOW_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                w.id, w.name, w.description, _marshal_models(w.steps),
                _marshal_models(w.triggers), _marshal_map(w.variables),
                bool_to_int(w.enabled), w.run_token, bool_to_int(w.protected),
                w.created_at, w.updated_at,
            ),
        )

    def get_workflow(self, id: str) -> Workflow:
        """Return the workflow with the given id, raising NotFoundError if absent."""
        row = self._one(
            f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (id,)
        )
        if row is None:
            raise NotFoundError("get workflow: no rows in result set")
        return _row_to_workflow(row)

    def get_workflow_by_run_token(self, token: str) -> Workflow:
        """Return the workflow whose public run token matches.

        An empty token never matches. Falls back to scanning workflows for an
        enabled webhook trigger whose config "token" matches, since that token
        lives inside the triggers JSON blob and is not SQL-queryable. Raises
        NotFoundError when nothing matches.
        """
        if token == "":
            raise NotFoundError("get workflow: no rows in result set")

        row = self._one(
            f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE run_token = ?", (token,)
        )
        if row is not None:
            return _row_to_workflow(row)

        # No workflow-level run_token match: the token may belong to a "webhook" trigger,
        # whose token lives inside the triggers JSON blob (not SQL-queryable).
        for wf in self.list_workflows():
            for t in wf.triggers:
                if t.enabled and t.type == "webhook" and t.config.get("token") == token:
                    return wf
        raise NotFoundError("get workflow: no rows in result set")

    def list_workflows(self) -> list[Workflow]:
        """Return all workflows ordered by creation time."""
        rows = self._query(
            f"SELECT {_WORKFLOW_COLUMNS} FROM workflows ORDER BY created_at"
        )
        return [_row_to_workflow(r) for r in rows]

    def update_workflow(self, w: Workflow) -> None:
        """Update the mutable fields of a workflow."""
        self._execute(
            "UPDATE workflows SET name = ?, description = ?, steps = ?, "
            "triggers = ?, variables = ?, enabled = ?, run_token = ?, "
            "protected = ?, updated_at = ? WHERE id = ?",
            (
                w.name, w.description, _marshal_models(w.steps),
                _marshal_models(w.triggers), _marshal_map(w.variables),
                bool_to_int(w.enabled), w.run_token, bool_to_int(w.protected),
                w.updated_at, w.id,
            ),
        )

    def update_workflow_triggers(
        self, id: str, triggers: list[WorkflowTrigger]
    ) -> None:
        """Persist ONLY the triggers column for a workflow.

        The poller uses it to save updated per-trigger state without racing a
        concurrent workflow edit on the rest of the record.
        """
        self._execute(
            "UPDATE workflows SET triggers = ? WHERE id = ?",
            (_marshal_models(triggers), id),
        )

    def delete_workflow(self, id: str) -> None:
        """Remove a workflow and its run history by id."""
        self._execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (id,))
        self._execute("DELETE FROM workflows WHERE id = ?", (id,))

    def create_run(self, run: WorkflowRun) -> None:
        """Insert a new workflow run record."""
        self._execute(
            f"INSERT INTO workflow_runs ({_RUN_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.id, run.workflow_id, run.trigger, run.status,
                _marshal_models(run.steps), run.started_at, run.finished_at,
            ),
        )

    def update_run(self, run: WorkflowRun) -> None:
        """Persist the mutable fields of a workflow run."""
        self._execute(
            "UPDATE workflow_runs SET trigger = ?, status = ?, steps = ?, "
            "started_at = ?, finished_at = ? WHERE id = ?",
            (
                run.trigger, run.status, _marshal_models(run.steps),
                run.started_at, run.finished_at, run.id,
            ),
        )

    def get_run(self, id: str) -> WorkflowRun:
        """Return the workflow run with the given id, raising NotFoundError if absent."""
        row = self._one(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE id = ?", (id,)
        )
        if row is None:
            raise NotFoundError("get workflow run: no rows in result set")
        return _row_to_run(row)

    def prune_workflow_runs(self, keep: int) -> int:
        """Keep only the newest ``keep`` runs of every workflow; return rows removed.

        Ranks each workflow's runs newest-first with a window function over
        ``idx_workflow_runs_wf`` (workflow_id, started_at DESC) and deletes the
        tail. Step results/outputs live inline in the run row (``steps`` blob),
        so deleting the row removes them too. A run still ``running`` is never
        deleted, even if it has fallen past the cut. ``keep <= 0`` is a no-op.
        """
        if keep <= 0:
            return 0
        cur = self._execute(
            "DELETE FROM workflow_runs WHERE id IN ("
            " SELECT id FROM ("
            "  SELECT id, status, ROW_NUMBER() OVER ("
            "   PARTITION BY workflow_id ORDER BY started_at DESC, id DESC"
            "  ) AS rn FROM workflow_runs"
            " ) WHERE rn > ? AND status != ?"
            ")",
            (keep, WorkflowRunRunning),
        )
        return cur.rowcount or 0

    def list_runs(self, workflow_id: str, limit: int) -> list[WorkflowRun]:
        """Return recent runs for a workflow, newest first (limit <= 0 => default 50)."""
        if limit <= 0:
            limit = 50
        rows = self._query(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs "
            "WHERE workflow_id = ? ORDER BY started_at DESC LIMIT ?",
            (workflow_id, limit),
        )
        return [_row_to_run(r) for r in rows]
