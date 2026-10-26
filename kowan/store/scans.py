"""Image vulnerability scan results (``image_scans`` table), see docs/scanning.md.

Provides :class:`_ScanMixin`, folded into :class:`kowan.store.store.Store`.
One row per image digest (the local image id, ``sha256:...``): the latest scan
of that content wins, whichever host ran it. Records are API-shaped dicts::

    {imageId, imageRef, repoDigests, hostId, status ("ok"|"error"), error,
     scanner, scannerVersion, os, counts: {critical, high, medium, low, unknown},
     total, fixable, findings: [...], scannedAt, durationMs}
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_SUMMARY_COLS = (
    "image_id, image_ref, repo_digests, host_id, status, error, scanner, scanner_version, os, "
    "critical, high, medium, low, unknown, total, fixable, scanned_at, duration_ms"
)


def _row(r: Any, with_findings: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "imageId": r["image_id"],
        "imageRef": r["image_ref"],
        "repoDigests": json.loads(r["repo_digests"] or "[]"),
        "hostId": r["host_id"],
        "status": r["status"],
        "error": r["error"],
        "scanner": r["scanner"],
        "scannerVersion": r["scanner_version"],
        "os": r["os"],
        "counts": {
            "critical": r["critical"], "high": r["high"], "medium": r["medium"],
            "low": r["low"], "unknown": r["unknown"],
        },
        "total": r["total"],
        "fixable": r["fixable"],
        "scannedAt": r["scanned_at"],
        "durationMs": r["duration_ms"],
    }
    if with_findings:
        out["findings"] = json.loads(r["findings"] or "[]")
    return out


class _ScanMixin(_Base):
    def upsert_image_scan(self, rec: Dict[str, Any]) -> None:
        counts = rec.get("counts") or {}
        self._execute(
            "INSERT INTO image_scans (image_id, image_ref, repo_digests, host_id, status, error, scanner, "
            "scanner_version, os, critical, high, medium, low, unknown, total, fixable, findings, scanned_at, duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(image_id) DO UPDATE SET image_ref=excluded.image_ref, repo_digests=excluded.repo_digests, "
            "host_id=excluded.host_id, status=excluded.status, error=excluded.error, scanner=excluded.scanner, "
            "scanner_version=excluded.scanner_version, os=excluded.os, critical=excluded.critical, high=excluded.high, "
            "medium=excluded.medium, low=excluded.low, unknown=excluded.unknown, total=excluded.total, "
            "fixable=excluded.fixable, findings=excluded.findings, scanned_at=excluded.scanned_at, "
            "duration_ms=excluded.duration_ms",
            (
                rec["imageId"], rec.get("imageRef", ""), json.dumps(rec.get("repoDigests") or []),
                rec.get("hostId", ""), rec.get("status", "ok"), rec.get("error", ""), rec.get("scanner", "trivy"),
                rec.get("scannerVersion", ""), rec.get("os", ""),
                int(counts.get("critical", 0)), int(counts.get("high", 0)), int(counts.get("medium", 0)),
                int(counts.get("low", 0)), int(counts.get("unknown", 0)),
                int(rec.get("total", 0)), int(rec.get("fixable", 0)),
                json.dumps(rec.get("findings") or []), rec.get("scannedAt", ""), int(rec.get("durationMs", 0)),
            ),
        )

    def get_image_scan(self, image_id: str, with_findings: bool = True) -> Optional[Dict[str, Any]]:
        cols = _SUMMARY_COLS + (", findings" if with_findings else "")
        r = self._one(f"SELECT {cols} FROM image_scans WHERE image_id = ?", (image_id,))
        return _row(r, with_findings) if r is not None else None

    def list_image_scans(self, image_ids: Optional[Iterable[str]] = None, host_id: str = "") -> List[Dict[str, Any]]:
        """Summaries (no findings) for the given image ids, or for a host, or all."""
        if image_ids is not None:
            ids = [i for i in image_ids if i]
            out: List[Dict[str, Any]] = []
            # Chunk to stay under SQLite's host-parameter limit.
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._query(f"SELECT {_SUMMARY_COLS} FROM image_scans WHERE image_id IN ({marks})", chunk)
                out.extend(_row(r, False) for r in rows)
            return out
        if host_id:
            rows = self._query(
                f"SELECT {_SUMMARY_COLS} FROM image_scans WHERE host_id = ? ORDER BY scanned_at DESC", (host_id,)
            )
        else:
            rows = self._query(f"SELECT {_SUMMARY_COLS} FROM image_scans ORDER BY scanned_at DESC")
        return [_row(r, False) for r in rows]
