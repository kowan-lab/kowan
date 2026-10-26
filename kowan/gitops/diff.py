"""Field-level and line-level diffs between canonical specs.

:func:`field_changes` walks two canonical specs and lists every leaf that
differs as ``{path, before, after}`` (lists of equal length are compared item
by item, otherwise as a whole). :func:`line_diff` renders the human-readable
diff the plan view shows: both specs as YAML documents compared line by line
with :mod:`difflib`, each line tagged ``" "`` / ``"+"`` / ``"-"``.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from .resources import dump_yaml, render_document

_MISSING = object()


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def field_changes(before: Any, after: Any, path: str = "") -> List[Dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        out: List[Dict[str, Any]] = []
        for k in list(before.keys()) + [k for k in after.keys() if k not in before]:
            b = before.get(k, _MISSING)
            a = after.get(k, _MISSING)
            if b is _MISSING:
                out.append({"path": _join(path, k), "before": None, "after": a})
            elif a is _MISSING:
                out.append({"path": _join(path, k), "before": b, "after": None})
            else:
                out.extend(field_changes(b, a, _join(path, k)))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            out.extend(field_changes(b, a, _join(path, i)))
        return out
    if before != after:
        return [{"path": path, "before": before, "after": after}]
    return []


def _doc_lines(kind: str, name: str, spec: Optional[Dict[str, Any]]) -> List[str]:
    if spec is None:
        return []
    return dump_yaml(render_document(kind, name, spec)).splitlines()


def line_diff(kind: str, name: str, before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Full-context line diff of the two resource documents."""
    a = _doc_lines(kind, name, before)
    b = _doc_lines(kind, name, after)
    out: List[Dict[str, str]] = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend({"op": " ", "text": t} for t in a[i1:i2])
            continue
        if tag in ("replace", "delete"):
            out.extend({"op": "-", "text": t} for t in a[i1:i2])
        if tag in ("replace", "insert"):
            out.extend({"op": "+", "text": t} for t in b[j1:j2])
    return out
