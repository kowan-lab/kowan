"""Auto-update policies, per-object state and history, see docs/labels.md.

Provides :class:`_AutoUpdateMixin`, folded into :class:`kowan.store.store.Store`.

  * ``autoupdate_policies`` - stored policy per object. ``kind`` is
    ``container`` (``target`` = container NAME, stable across recreates) or
    ``deployment`` (``target`` = deployment id; ``host_id`` = '').
  * ``autoupdate_state``    - what the background checker last saw for an
    object (JSON blob: digests, first-seen time, last result, ...).
  * ``autoupdate_history``  - one row per noteworthy outcome (updated,
    rolled_back, blocked, notified, pulled, failed).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_POLICY_COLS = "kind, host_id, target, mode, win, min_age, scan_block, updated_at, updated_by"
_HIST_COLS = (
    "id, kind, host_id, target, target_name, image, from_digest, to_digest, mode, trig, outcome, "
    "message, scan, started_at, finished_at"
)


def _policy(r: Any) -> Dict[str, Any]:
    return {
        "kind": r["kind"], "hostId": r["host_id"], "target": r["target"], "mode": r["mode"],
        "window": r["win"], "minAge": r["min_age"], "scanBlock": r["scan_block"],
        "updatedAt": r["updated_at"], "updatedBy": r["updated_by"],
    }


def _hist(r: Any) -> Dict[str, Any]:
    scan = None
    if r["scan"]:
        try:
            scan = json.loads(r["scan"])
        except ValueError:
            scan = None
    return {
        "id": r["id"], "kind": r["kind"], "hostId": r["host_id"], "target": r["target"],
        "targetName": r["target_name"], "image": r["image"], "fromDigest": r["from_digest"],
        "toDigest": r["to_digest"], "mode": r["mode"], "trigger": r["trig"], "outcome": r["outcome"],
        "message": r["message"], "scan": scan, "startedAt": r["started_at"], "finishedAt": r["finished_at"],
    }


class _AutoUpdateMixin(_Base):
    # -- policies ------------------------------------------------------------
    def upsert_autoupdate_policy(self, p: Dict[str, Any]) -> None:
        self._execute(
            f"INSERT INTO autoupdate_policies ({_POLICY_COLS}) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, host_id, target) DO UPDATE SET mode=excluded.mode, win=excluded.win, "
            "min_age=excluded.min_age, scan_block=excluded.scan_block, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            (
                p["kind"], p.get("hostId", ""), p["target"], p.get("mode", "off"), p.get("window", ""),
                p.get("minAge", ""), p.get("scanBlock", ""), p.get("updatedAt", ""), p.get("updatedBy", ""),
            ),
        )

    def get_autoupdate_policy(self, kind: str, host_id: str, target: str) -> Optional[Dict[str, Any]]:
        r = self._one(
            f"SELECT {_POLICY_COLS} FROM autoupdate_policies WHERE kind = ? AND host_id = ? AND target = ?",
            (kind, host_id, target),
        )
        return _policy(r) if r is not None else None

    def list_autoupdate_policies(self, kind: str = "", host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = f"SELECT {_POLICY_COLS} FROM autoupdate_policies WHERE 1=1"
        args: List[Any] = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if host_id is not None:
            sql += " AND host_id = ?"
            args.append(host_id)
        return [_policy(r) for r in self._query(sql + " ORDER BY kind, host_id, target", args)]

    def delete_autoupdate_policy(self, kind: str, host_id: str, target: str) -> None:
        self._execute(
            "DELETE FROM autoupdate_policies WHERE kind = ? AND host_id = ? AND target = ?", (kind, host_id, target)
        )

    # -- state ---------------------------------------------------------------
    def get_autoupdate_state(self, kind: str, host_id: str, target: str) -> Dict[str, Any]:
        r = self._one(
            "SELECT data FROM autoupdate_state WHERE kind = ? AND host_id = ? AND target = ?", (kind, host_id, target)
        )
        if r is None:
            return {}
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def put_autoupdate_state(self, kind: str, host_id: str, target: str, data: Dict[str, Any], now: str) -> None:
        self._execute(
            "INSERT INTO autoupdate_state (kind, host_id, target, data, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, host_id, target) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (kind, host_id, target, json.dumps(data), now),
        )

    def list_autoupdate_states(self) -> List[Dict[str, Any]]:
        out = []
        for r in self._query("SELECT kind, host_id, target, data, updated_at FROM autoupdate_state"):
            try:
                data = json.loads(r["data"] or "{}")
            except ValueError:
                data = {}
            out.append({"kind": r["kind"], "hostId": r["host_id"], "target": r["target"],
                        "data": data if isinstance(data, dict) else {}, "updatedAt": r["updated_at"]})
        return out

    def delete_autoupdate_state(self, kind: str, host_id: str, target: str) -> None:
        self._execute(
            "DELETE FROM autoupdate_state WHERE kind = ? AND host_id = ? AND target = ?", (kind, host_id, target)
        )

    # -- history -------------------------------------------------------------
    def add_autoupdate_history(self, h: Dict[str, Any]) -> None:
        self._execute(
            f"INSERT INTO autoupdate_history ({_HIST_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                h["id"], h["kind"], h.get("hostId", ""), h["target"], h.get("targetName", ""), h.get("image", ""),
                h.get("fromDigest", ""), h.get("toDigest", ""), h.get("mode", ""), h.get("trigger", "auto"),
                h["outcome"], h.get("message", "")[:2000], json.dumps(h["scan"]) if h.get("scan") else "",
                h.get("startedAt", ""), h.get("finishedAt", ""),
            ),
        )

    def list_autoupdate_history(
        self, *, kind: str = "", host_id: str = "", target: str = "", outcome: str = "",
        limit: int = 50, offset: int = 0,
    ) -> Dict[str, Any]:
        where = " WHERE 1=1"
        args: List[Any] = []
        for col, val in (("kind", kind), ("host_id", host_id), ("target", target), ("outcome", outcome)):
            if val:
                where += f" AND {col} = ?"
                args.append(val)
        total = self._one("SELECT COUNT(*) AS n FROM autoupdate_history" + where, args)
        rows = self._query(
            f"SELECT {_HIST_COLS} FROM autoupdate_history{where} ORDER BY started_at DESC, id LIMIT ? OFFSET ?",
            args + [max(1, min(int(limit), 500)), max(0, int(offset))],
        )
        return {"items": [_hist(r) for r in rows], "total": int(total["n"]) if total else 0}

    def last_autoupdate_history(self, kind: str, host_id: str, target: str) -> Optional[Dict[str, Any]]:
        r = self._one(
            f"SELECT {_HIST_COLS} FROM autoupdate_history WHERE kind = ? AND host_id = ? AND target = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (kind, host_id, target),
        )
        return _hist(r) if r is not None else None

    def prune_autoupdate_history(self, before: str) -> int:
        cur = self._execute("DELETE FROM autoupdate_history WHERE started_at < ?", (before,))
        return int(cur.rowcount or 0)
