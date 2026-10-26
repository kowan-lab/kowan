"""The update plan (orchestrator -> helper) and the helper journal (helper -> reconciler).

The plan travels as ONE command-line argument of the helper container
(``self-update --plan <base64url(json)>``). Keeping it in the container's own
``Config.Cmd`` means the helper container is self-describing: the reconciler can
rebuild the full ``updates`` record from the helper alone (plan + logs), even
when the server database was restored from a snapshot taken before the record
reached its final shape.

The helper writes one JSON object per line on stdout::

    {"ts": "...", "id": "...", "phase": "stop", "ok": true, "detail": "..."}

and a final line ``{"result": "updated" | "rolled_back" | "failed" | "aborted",
...}``. Docker may prefix each log line with an RFC3339 timestamp (``docker logs
-t`` / the Engine ``timestamps=1`` flag); :func:`parse_journal` tolerates both.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import COMPONENTS, MODE_ROLLBACK, MODE_UPDATE

PLAN_VERSION = 1

DEFAULTS: Dict[str, Any] = {
    "waitSeconds": 3.0,
    "stopTimeout": 20,
    "verifyTimeout": 120.0,
    "stableSeconds": 10.0,
    "pollInterval": 1.0,
    "logTail": 200,
}


class PlanError(ValueError):
    """The plan is malformed (bad base64/JSON or a missing/invalid field)."""


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate ``plan`` and fill defaults. Returns a new dict; raises PlanError."""
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    out = dict(DEFAULTS)
    out.update({k: v for k, v in plan.items() if v is not None})
    if int(out.get("v", PLAN_VERSION)) != PLAN_VERSION:
        raise PlanError(f"unsupported plan version {out.get('v')!r}")
    out["v"] = PLAN_VERSION
    for key in ("id", "component", "mode", "target"):
        if not isinstance(out.get(key), str) or not out[key]:
            raise PlanError(f"plan.{key} is required")
    if out["component"] not in COMPONENTS:
        raise PlanError(f"plan.component must be one of {COMPONENTS}")
    if out["mode"] not in (MODE_UPDATE, MODE_ROLLBACK):
        raise PlanError("plan.mode must be 'update' or 'rollback'")
    if out["mode"] == MODE_UPDATE and not (out.get("image") or out.get("imageId")):
        raise PlanError("plan.image is required for an update")
    if not out.get("previous"):
        out["previous"] = f"{out['target']}-previous"
    for key in ("waitSeconds", "verifyTimeout", "stableSeconds", "pollInterval"):
        try:
            out[key] = float(out[key])
        except (TypeError, ValueError) as err:
            raise PlanError(f"plan.{key} must be a number") from err
        if out[key] < 0:
            raise PlanError(f"plan.{key} must be >= 0")
    for key in ("stopTimeout", "logTail"):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError) as err:
            raise PlanError(f"plan.{key} must be an integer") from err
    db = out.get("db")
    if db is not None:
        if not isinstance(db, dict) or not db.get("path"):
            raise PlanError("plan.db.path is required when plan.db is set")
    rec = out.get("record")
    if rec is not None and not isinstance(rec, dict):
        raise PlanError("plan.record must be an object")
    return out


def encode_plan(plan: Dict[str, Any]) -> str:
    """Encode a (validated) plan as compact base64url JSON (no padding)."""
    raw = json.dumps(normalize_plan(plan), separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_plan(token: str) -> Dict[str, Any]:
    """Inverse of :func:`encode_plan`. Raises :class:`PlanError`."""
    if not isinstance(token, str) or not token.strip():
        raise PlanError("empty plan")
    s = token.strip()
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.urlsafe_b64decode(s.encode("ascii"))
        doc = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError) as err:
        raise PlanError(f"invalid plan encoding: {err}") from err
    return normalize_plan(doc)


def plan_from_cmd(cmd: Any) -> Optional[Dict[str, Any]]:
    """Extract the plan from a helper container's ``Config.Cmd`` (or None)."""
    if not isinstance(cmd, list):
        return None
    for i, arg in enumerate(cmd):
        if arg == "--plan" and i + 1 < len(cmd):
            try:
                return decode_plan(cmd[i + 1])
            except PlanError:
                return None
        if isinstance(arg, str) and arg.startswith("--plan="):
            try:
                return decode_plan(arg[len("--plan="):])
            except PlanError:
                return None
    return None


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #


def journal_line(update_id: str, phase: str, ok: bool, detail: str = "", ts: str = "") -> str:
    return json.dumps(
        {"ts": ts or now_rfc3339(), "id": update_id, "phase": phase, "ok": bool(ok),
         "detail": detail or ""},
        separators=(",", ":"),
    )


def result_line(result: Dict[str, Any]) -> str:
    return json.dumps(result, separators=(",", ":"), default=str)


def _strip_ts(line: str) -> str:
    """Drop a leading Docker log timestamp (``2026-10-25T10:00:00.123Z ``)."""
    s = line.strip()
    if s.startswith("{"):
        return s
    head, sep, rest = s.partition(" ")
    if sep and len(head) >= 20 and head[:4].isdigit() and head[4] == "-" and "T" in head:
        return rest.strip()
    return s


def parse_journal(text: str) -> Dict[str, Any]:
    """Parse helper stdout into ``{"phases": [...], "result": dict|None, "other": [...]}``.

    ``phases`` items use the API shape ``{phase, ok, at, detail}``. Non-JSON lines
    (Python tracebacks, log records on stderr) are kept in ``other`` so a crashed
    helper still leaves a readable trail. The LAST result line wins.
    """
    phases: List[Dict[str, Any]] = []
    other: List[str] = []
    result: Optional[Dict[str, Any]] = None
    for raw in (text or "").splitlines():
        line = _strip_ts(raw)
        if not line:
            continue
        doc: Any = None
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                doc = None
        if not isinstance(doc, dict):
            other.append(line)
            continue
        if "result" in doc:
            result = doc
        elif "phase" in doc:
            phases.append(
                {
                    "phase": str(doc.get("phase") or ""),
                    "ok": bool(doc.get("ok")),
                    "at": str(doc.get("ts") or ""),
                    "detail": str(doc.get("detail") or ""),
                }
            )
        else:
            other.append(line)
    return {"phases": phases, "result": result, "other": other}
