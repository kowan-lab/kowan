"""Apply a diagnostic's fix patch.

The UI applies patches with the ``yaml`` npm package's Document API, which
keeps comments and formatting. This server-side twin works on plain data (the
round trip through PyYAML drops comments), so it is used by tests and by
non-interactive callers; both follow the same operation semantics:

* ``set``: walk ``path``, creating missing mappings, and set the last segment.
* ``remove``: delete the mapping key or sequence item (no-op when absent).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import yaml

from kowan import yamlsafe


def apply_patch_data(doc: Any, patch: List[Dict[str, Any]]) -> Any:
    doc = copy.deepcopy(doc)
    for op in patch:
        path = list(op.get("path") or [])
        if not path:
            continue
        parent = doc
        for seg in path[:-1]:
            if isinstance(parent, list):
                parent = parent[int(seg)]
            else:
                if seg not in parent or parent[seg] is None:
                    if op.get("op") == "remove":
                        parent = None
                        break
                    parent[seg] = {}
                parent = parent[seg]
        if parent is None:
            continue
        last = path[-1]
        if op.get("op") == "set":
            if isinstance(parent, list):
                parent[int(last)] = op.get("value")
            else:
                parent[last] = op.get("value")
        elif op.get("op") == "remove":
            if isinstance(parent, list):
                if 0 <= int(last) < len(parent):
                    parent.pop(int(last))
            elif isinstance(parent, dict):
                parent.pop(last, None)
    return doc


def apply_patch(text: str, patch: List[Dict[str, Any]]) -> str:
    """Apply ``patch`` to compose ``text`` (comments are not preserved)."""
    doc = yamlsafe.safe_load(text) or {}
    return yaml.safe_dump(apply_patch_data(doc, patch), sort_keys=False, default_flow_style=False)
