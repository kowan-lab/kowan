"""Custom app-template CRUD (the admin-created part of the template catalog).

Provides :class:`_TemplateMixin`, folded into :class:`kowan.store.store.Store`.
Built-in templates are never stored here (they ship in
``kowan/templates_builtin.json``); every scanned row is a custom template.

Columns: ``spec`` holds the raw container spec as JSON (it may contain
``${VAR}`` tokens, so it is NOT validated into a ContainerSpec here) and is
NULL for compose templates; ``inputs`` holds the input declarations as a JSON
array.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, List

from kowan.templates import Template, TemplateInput

from ._common import NotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_TEMPLATE_COLUMNS = (
    "id, name, description, category, icon, type, spec, compose, inputs, "
    "website, created_at, updated_at"
)


def _row_to_template(row: sqlite3.Row) -> Template:
    spec = json.loads(row["spec"]) if row["spec"] else None
    try:
        inputs_raw = json.loads(row["inputs"] or "[]")
    except (TypeError, ValueError):
        inputs_raw = []
    return Template(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        category=row["category"],
        icon=row["icon"],
        type=row["type"],
        spec=spec if isinstance(spec, dict) else None,
        compose=row["compose"],
        inputs=[TemplateInput.model_validate(i) for i in inputs_raw if isinstance(i, dict)],
        website=row["website"],
        builtIn=False,
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    )


def _params(t: Template) -> tuple:
    return (
        t.name, t.description, t.category, t.icon, t.type,
        json.dumps(t.spec, separators=(",", ":")) if t.spec is not None else None,
        t.compose,
        json.dumps([i.model_dump(by_alias=True) for i in t.inputs], separators=(",", ":")),
        t.website,
    )


class _TemplateMixin(_Base):
    def create_template(self, t: Template) -> None:
        """Insert a new custom template (``t.created_at``/``updated_at`` set by caller)."""
        self._execute(
            f"INSERT INTO templates ({_TEMPLATE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, *_params(t), t.created_at, t.updated_at),
        )

    def get_template(self, id: str) -> Template:
        """Return the custom template ``id``; raise NotFoundError if absent."""
        row = self._one(f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get template: no rows in result set")
        return _row_to_template(row)

    def list_templates(self) -> List[Template]:
        """Return all custom templates ordered by name."""
        rows = self._query(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates ORDER BY name COLLATE NOCASE"
        )
        return [_row_to_template(r) for r in rows]

    def update_template(self, t: Template) -> None:
        """Replace the mutable fields of custom template ``t.id``."""
        cur = self._execute(
            "UPDATE templates SET name = ?, description = ?, category = ?, icon = ?, "
            "type = ?, spec = ?, compose = ?, inputs = ?, website = ?, updated_at = ? "
            "WHERE id = ?",
            (*_params(t), t.updated_at, t.id),
        )
        if cur.rowcount == 0:
            raise NotFoundError("update template: no rows affected")

    def delete_template(self, id: str) -> None:
        """Remove custom template ``id``; raise NotFoundError if absent."""
        cur = self._execute("DELETE FROM templates WHERE id = ?", (id,))
        if cur.rowcount == 0:
            raise NotFoundError("delete template: no rows affected")
