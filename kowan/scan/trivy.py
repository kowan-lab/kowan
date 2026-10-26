"""Pure Trivy helpers: container spec, log decoding and report parsing.

Trivy runs as ``aquasec/trivy:<pinned>`` on the *target* host with:

  * the host's docker socket bind-mounted read-only, so ``--image-src docker``
    reads the image straight from that daemon (no registry pull, local-only
    images work);
  * a named volume (``kowan-trivy-cache``) mounted at ``/cache`` so the
    vulnerability DB (~70 MB) is downloaded once per host and then re-used;
  * ``image --format json --scanners vuln --quiet`` so stdout carries only the
    JSON report (``--quiet`` silences the progress log on stderr).

The report is read back from the container's logs. ``Ops.container_logs``
returns lines prefixed with an RFC3339 timestamp (the API always asks docker
for timestamps), so :func:`strip_log_timestamps` removes them before parsing.

Everything here is I/O free; ``tests/test_scan.py`` feeds it a recorded report.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Pinned scanner image. Override with KOWAN_TRIVY_IMAGE (e.g. a mirror).
DEFAULT_TRIVY_IMAGE = "aquasec/trivy:0.75.0"
# Named volume holding Trivy's DB/cache on each host.
DEFAULT_CACHE_VOLUME = "kowan-trivy-cache"
# Name prefix of the short-lived scan containers.
DEFAULT_CONTAINER_PREFIX = "kowan-trivy-"
# Findings kept per image (sorted most severe first). Counts are always exact.
MAX_FINDINGS = 1000

SEVERITIES: Tuple[str, ...] = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

_TS_PREFIX = re.compile(rb"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d) ")


def trivy_image() -> str:
    return os.environ.get("KOWAN_TRIVY_IMAGE", "").strip() or DEFAULT_TRIVY_IMAGE


def cache_volume() -> str:
    return os.environ.get("KOWAN_TRIVY_CACHE_VOLUME", "").strip() or DEFAULT_CACHE_VOLUME


def container_prefix() -> str:
    return os.environ.get("KOWAN_TRIVY_CONTAINER_PREFIX", "").strip() or DEFAULT_CONTAINER_PREFIX


def scanner_version_from_image(image: str) -> str:
    """``aquasec/trivy:0.75.0`` -> ``0.75.0`` (fallback when the report has none)."""
    tail = image.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else ""


def trivy_command(target: str, timeout: str = "15m") -> List[str]:
    """Arguments for the Trivy container (its entrypoint is ``trivy``)."""
    return [
        "image",
        "--format", "json",
        "--scanners", "vuln",
        "--quiet",
        "--image-src", "docker",
        "--cache-dir", "/cache",
        "--timeout", timeout,
        target,
    ]


def build_scan_spec(target: str, *, image: str = "", volume: str = "", socket: str = "/var/run/docker.sock") -> Any:
    """The :class:`ContainerSpec` for one scan of ``target`` (a ref or image id)."""
    from kowan.models.deployment import ContainerSpec, VolumeMapping

    return ContainerSpec(
        image=image or trivy_image(),
        command=trivy_command(target),
        volumes=[
            VolumeMapping(source=socket, target="/var/run/docker.sock", readOnly=True),
            VolumeMapping(source=volume or cache_volume(), target="/cache"),
        ],
        # kowan.support=true hides it from the default Containers view.
        labels={"kowan.support": "true", "kowan.scan": "trivy"},
    )


def strip_log_timestamps(raw: bytes) -> bytes:
    """Drop the per-line ``2026-01-01T00:00:00.000000000Z `` prefix docker adds."""
    out = []
    for line in raw.split(b"\n"):
        out.append(_TS_PREFIX.sub(b"", line, count=1))
    return b"\n".join(out)


def extract_json(raw: bytes) -> Dict[str, Any]:
    """Parse the JSON object in Trivy's stdout (tolerates leading/trailing noise)."""
    text = strip_log_timestamps(raw).decode("utf-8", "replace")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        snippet = text.strip().splitlines()[-1:] if text.strip() else []
        raise ValueError("trivy produced no JSON report" + (f": {snippet[0][:300]}" if snippet else ""))
    try:
        doc = json.loads(text[start:end + 1])
    except ValueError as err:
        raise ValueError(f"invalid trivy JSON: {err}") from err
    if not isinstance(doc, dict):
        raise ValueError("invalid trivy JSON: not an object")
    return doc


@dataclass
class ScanSummary:
    """Normalised result of one scan (what the store and the UI use)."""

    image_id: str = ""
    artifact: str = ""
    repo_digests: List[str] = field(default_factory=list)
    os: str = ""
    scanner_version: str = ""
    counts: Dict[str, int] = field(default_factory=lambda: {s.lower(): 0 for s in SEVERITIES})
    total: int = 0
    fixable: int = 0
    findings: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def critical(self) -> int:
        return self.counts.get("critical", 0)

    @property
    def high(self) -> int:
        return self.counts.get("high", 0)


def _sev(v: Any) -> str:
    s = str(v or "").upper()
    return s if s in _RANK else "UNKNOWN"


def parse_trivy_report(doc: Dict[str, Any], *, max_findings: int = MAX_FINDINGS) -> ScanSummary:
    """Fold a Trivy JSON report into a :class:`ScanSummary`.

    The same (CVE, package, installed version) can appear in several targets
    (e.g. a jar copied twice); it is counted once. Findings are sorted by
    severity, then fixable first, then id, and capped at ``max_findings``.
    """
    meta = doc.get("Metadata") or {}
    os_info = meta.get("OS") or {}
    out = ScanSummary(
        image_id=str(meta.get("ImageID") or ""),
        artifact=str(doc.get("ArtifactName") or ""),
        repo_digests=[str(d) for d in (meta.get("RepoDigests") or [])],
        os=" ".join(x for x in (str(os_info.get("Family") or ""), str(os_info.get("Name") or "")) if x),
        scanner_version=str(((doc.get("Trivy") or {}).get("Version")) or ""),
    )
    seen = set()
    rows: List[Dict[str, Any]] = []
    for res in doc.get("Results") or []:
        target = str(res.get("Target") or "")
        for v in res.get("Vulnerabilities") or []:
            vid = str(v.get("VulnerabilityID") or "")
            pkg = str(v.get("PkgName") or "")
            installed = str(v.get("InstalledVersion") or "")
            key = (vid, pkg, installed)
            if key in seen:
                continue
            seen.add(key)
            sev = _sev(v.get("Severity"))
            out.counts[sev.lower()] = out.counts.get(sev.lower(), 0) + 1
            fixed = str(v.get("FixedVersion") or "")
            if fixed:
                out.fixable += 1
            rows.append({
                "id": vid,
                "pkg": pkg,
                "installed": installed,
                "fixed": fixed,
                "severity": sev,
                "title": str(v.get("Title") or "")[:300],
                "url": str(v.get("PrimaryURL") or ""),
                "target": target[:200],
            })
    out.total = len(rows)
    rows.sort(key=lambda r: (_RANK[r["severity"]], 0 if r["fixed"] else 1, r["id"], r["pkg"]))
    out.findings = rows[:max_findings]
    return out


def counts_tuple(counts: Optional[Dict[str, int]]) -> Tuple[int, int, int, int]:
    c = counts or {}
    return (int(c.get("critical", 0)), int(c.get("high", 0)), int(c.get("medium", 0)), int(c.get("low", 0)))


def sum_counts(items: Iterable[Optional[Dict[str, int]]]) -> Dict[str, int]:
    tot = {s.lower(): 0 for s in SEVERITIES}
    for c in items:
        for k, v in (c or {}).items():
            if k in tot:
                tot[k] += int(v or 0)
    return tot
