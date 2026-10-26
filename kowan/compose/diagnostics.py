"""The diagnostic record every compose check produces.

One stable JSON shape for the API and the UI::

    {"severity": "error"|"warning"|"info", "code": "kowan-in-use",
     "message": "...", "path": "services.web.ports[0]", "line": 7, "column": 7,
     "fix": {"description": "...", "patch": [{"op": "set", "path": [...], "value": ...}]}}

``line``/``column`` are 1-based and ``0`` when unknown. ``fix`` is present only
when a safe automatic change exists; ``patch`` is a list of operations on the
YAML *document* (not on the text), so the UI can apply it with a comment-
preserving YAML library and both editor modes stay in sync:

  * ``{"op": "set", "path": [..], "value": v}`` - create or replace the node.
  * ``{"op": "remove", "path": [..]}`` - delete a mapping key / sequence item.

Paths are lists of mapping keys (str) and sequence indexes (int).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_RANK = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

PathSeg = Union[str, int]


def format_path(path: Sequence[PathSeg]) -> str:
    """``["services", "web", "ports", 0]`` -> ``services.web.ports[0]``."""
    out = ""
    for seg in path:
        if isinstance(seg, int):
            out += f"[{seg}]"
        else:
            out += ("." if out else "") + str(seg)
    return out


@dataclass
class Fix:
    description: str
    patch: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"description": self.description, "patch": self.patch}


@dataclass
class Diagnostic:
    severity: str
    code: str
    message: str
    path: str = ""
    line: int = 0
    column: int = 0
    fix: Optional[Fix] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
        }
        if self.fix is not None:
            d["fix"] = self.fix.to_dict()
        return d


def sort_diagnostics(diags: List[Diagnostic]) -> List[Diagnostic]:
    """Stable order: by position (unknown last), then severity, then code."""
    return sorted(
        diags,
        key=lambda d: (d.line == 0, d.line, d.column, _RANK.get(d.severity, 3), d.code),
    )


def has_errors(diags: List[Diagnostic]) -> bool:
    return any(d.severity == SEVERITY_ERROR for d in diags)
