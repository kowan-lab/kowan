"""Serialization helpers shared by the pydantic models.

pydantic emits every field. :func:`drop_empty` drops the specific fields a model
marks as "omit when empty" when they hold a zero value, keeping the wire JSON
(and the JSON the store persists via ``model_dump``) compact.
"""

from __future__ import annotations

from typing import Any, Dict


def drop_empty(model: Any, data: Dict[str, Any], *attrs: str) -> Dict[str, Any]:
    """Remove ``attrs`` from ``data`` when their value on ``model`` is empty.

    ``attrs`` are python attribute names; both the attribute name and its field
    alias are removed, so the result is correct whether the model was dumped
    with ``by_alias=True`` or not. "Empty" means falsy (None, ``""``, ``0``,
    empty collection).
    """
    fields = type(model).model_fields
    for attr in attrs:
        if getattr(model, attr, None):
            continue
        fld = fields.get(attr)
        if fld is not None and fld.alias:
            data.pop(fld.alias, None)
        data.pop(attr, None)
    return data
