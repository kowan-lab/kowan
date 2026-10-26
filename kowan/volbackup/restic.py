"""Pure restic helpers: repository environment, retention, scripts, parsing.

Nothing here touches Docker or the store, so it is fully unit-testable.

The helper container runs ``/bin/sh -c <script>``. Scripts print marker lines
``@@kowan <word> ...`` between restic invocations (``@@kowan rc <phase> <name>
<code>`` carries each exit code), so one container run can do init + several
backups + forget and the log can still be split per phase afterwards.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

# Pinned image: upgrade deliberately (restic >= 0.17 for exit code 10 =
# "repository does not exist" and `restore --delete`).
RESTIC_IMAGE = "restic/restic:0.17.3"

REPO_TYPES = ("s3", "local", "sftp", "rest")

# Mount points inside the helper container.
DATA_ROOT = "/data"
REPO_MOUNT = "/repo"
TARGET_MOUNT = "/target"

MARK = "@@kowan"

RETENTION_KEYS = (
    ("keepLast", "--keep-last"),
    ("keepHourly", "--keep-hourly"),
    ("keepDaily", "--keep-daily"),
    ("keepWeekly", "--keep-weekly"),
    ("keepMonthly", "--keep-monthly"),
    ("keepYearly", "--keep-yearly"),
)

# Secret keys accepted per repository type (all repos need "password").
SECRET_KEYS = {
    "s3": ("password", "accessKeyId", "secretAccessKey"),
    "local": ("password",),
    "sftp": ("password", "sshKey"),
    "rest": ("password", "restPassword"),
}
CONFIG_KEYS = {
    "s3": ("endpoint", "bucket", "prefix", "region"),
    "local": ("path",),
    "sftp": ("host", "port", "user", "path"),
    "rest": ("url", "username"),
}
COMMON_CONFIG_KEYS = ("network",)

_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.:=@/+-]{1,64}$")
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\s?")


class RepoConfigError(ValueError):
    pass


def valid_volume_name(name: str) -> bool:
    return bool(_VOLUME_RE.match(name or ""))


def valid_tag(tag: str) -> bool:
    return bool(_TAG_RE.match(tag or "")) and "," not in tag


def volume_tag(volume: str) -> str:
    return f"kowan-volume={volume}"


def job_tag(job_id: str) -> str:
    return f"kowan-job={job_id}"


# ---------------------------------------------------------------------------
# repository -> container env / mounts
# ---------------------------------------------------------------------------


@dataclass
class RepoRuntime:
    """What the helper container needs to reach a repository."""

    env: Dict[str, str] = field(default_factory=dict)
    binds: List[Tuple[str, str, bool]] = field(default_factory=list)  # (source, target, ro)
    network: str = ""


def _strip_slashes(s: str) -> str:
    return (s or "").strip().strip("/")


def repo_runtime(repo: Dict[str, Any], secrets: Dict[str, str]) -> RepoRuntime:
    """Build env + binds for a repository record (config) and its decrypted
    secrets. Raises :class:`RepoConfigError` for an unusable record."""
    t = repo.get("type", "")
    cfg = repo.get("config") or {}
    password = secrets.get("password", "")
    if not password:
        raise RepoConfigError("repository password is not set")
    rt = RepoRuntime(env={"RESTIC_PASSWORD": password}, network=str(cfg.get("network", "") or "").strip())
    if t == "s3":
        bucket = _strip_slashes(cfg.get("bucket", ""))
        if not bucket:
            raise RepoConfigError("s3: bucket is required")
        endpoint = (cfg.get("endpoint") or "s3.amazonaws.com").strip().rstrip("/")
        prefix = _strip_slashes(cfg.get("prefix", ""))
        url = f"s3:{endpoint}/{bucket}" + (f"/{prefix}" if prefix else "")
        rt.env["RESTIC_REPOSITORY"] = url
        if secrets.get("accessKeyId"):
            rt.env["AWS_ACCESS_KEY_ID"] = secrets["accessKeyId"]
        if secrets.get("secretAccessKey"):
            rt.env["AWS_SECRET_ACCESS_KEY"] = secrets["secretAccessKey"]
        if cfg.get("region"):
            rt.env["AWS_DEFAULT_REGION"] = str(cfg["region"])
    elif t == "local":
        path = (cfg.get("path") or "").strip()
        if not path.startswith("/"):
            raise RepoConfigError("local: path must be an absolute path on the host")
        rt.binds.append((path, REPO_MOUNT, False))
        rt.env["RESTIC_REPOSITORY"] = REPO_MOUNT
    elif t == "sftp":
        host = (cfg.get("host") or "").strip()
        user = (cfg.get("user") or "").strip()
        path = (cfg.get("path") or "").strip()
        if not host or not user or not path:
            raise RepoConfigError("sftp: host, user and path are required")
        port = str(cfg.get("port") or "22").strip()
        if not port.isdigit():
            raise RepoConfigError("sftp: port must be a number")
        rt.env["RESTIC_REPOSITORY"] = f"sftp:{user}@{host}:{path}"
        key = secrets.get("sshKey", "")
        cmd = ["ssh", "-p", port, "-o", "StrictHostKeyChecking=accept-new",
               "-o", "UserKnownHostsFile=/tmp/known_hosts", "-o", "BatchMode=yes"]
        if key:
            rt.env["KOWAN_SSH_KEY"] = key
            cmd += ["-i", "/tmp/id_port"]
        cmd += [f"{user}@{host}", "-s", "sftp"]
        rt.env["KOWAN_SFTP_CMD"] = " ".join(shlex.quote(c) for c in cmd)
    elif t == "rest":
        url = (cfg.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RepoConfigError("rest: url must be http(s)://host[:port]/path")
        user = (cfg.get("username") or "").strip()
        pw = secrets.get("restPassword", "")
        netloc = parts.netloc.rsplit("@", 1)[-1]
        if user:
            cred = quote(user, safe="") + (":" + quote(pw, safe="") if pw else "")
            netloc = f"{cred}@{netloc}"
        rt.env["RESTIC_REPOSITORY"] = "rest:" + urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    else:
        raise RepoConfigError(f"unknown repository type {t!r}")
    return rt


def redact(text: str, secrets: Iterable[str]) -> str:
    """Blank every secret value out of ``text`` (logs are stored + shown)."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "********")
    return text


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


def retention_flags(retention: Optional[Dict[str, Any]]) -> List[str]:
    """``{"keepDaily": 7, "keepLast": 3}`` -> ``["--keep-last", "3", "--keep-daily", "7"]``.
    Zero / missing / invalid values are ignored; no flags = keep everything."""
    out: List[str] = []
    r = retention or {}
    for key, flag in RETENTION_KEYS:
        v = r.get(key)
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out += [flag, str(n)]
    return out


def normalize_retention(retention: Any) -> Dict[str, int]:
    if retention is None:
        return {}
    if not isinstance(retention, dict):
        raise ValueError("retention must be an object")
    out: Dict[str, int] = {}
    for key, _ in RETENTION_KEYS:
        if key in retention and retention[key] not in (None, ""):
            try:
                n = int(retention[key])
            except (TypeError, ValueError):
                raise ValueError(f"retention.{key} must be an integer")
            if n < 0 or n > 100000:
                raise ValueError(f"retention.{key} out of range")
            if n:
                out[key] = n
    unknown = set(retention) - {k for k, _ in RETENTION_KEYS}
    if unknown:
        raise ValueError("unknown retention key(s): " + ", ".join(sorted(unknown)))
    return out


# ---------------------------------------------------------------------------
# scripts
# ---------------------------------------------------------------------------

_PRELUDE = r"""set -u
if [ -n "${KOWAN_SSH_KEY:-}" ]; then umask 077; printf '%s\n' "$KOWAN_SSH_KEY" > /tmp/id_port; fi
r() { if [ -n "${KOWAN_SFTP_CMD:-}" ]; then restic -o "sftp.command=$KOWAN_SFTP_CMD" "$@"; else restic "$@"; fi; }
"""

_ENSURE_REPO = r"""echo "@@kowan phase open"
r cat config >/dev/null 2>/tmp/open.err
rc=$?
if [ "$rc" -eq 10 ]; then
  echo "@@kowan phase init"
  r init 2>&1
  rc=$?
  echo "@@kowan rc init repo $rc"
  [ "$rc" -eq 0 ] || exit 20
  echo "@@kowan initialized"
elif [ "$rc" -ne 0 ]; then
  cat /tmp/open.err
  echo "@@kowan rc open repo $rc"
  exit 21
fi
"""


def q(s: str) -> str:
    return shlex.quote(s)


def backup_script(volumes: List[str], *, job_id: str, host_label: str, tags: List[str],
                  retention: Optional[Dict[str, Any]]) -> str:
    """Init-if-needed, one ``restic backup`` per volume, then ``forget --prune``."""
    lines = [_PRELUDE, _ENSURE_REPO]
    base_tags = ["kowan", job_tag(job_id)] + [t for t in tags if valid_tag(t)]
    for v in volumes:
        if not valid_volume_name(v):
            raise ValueError(f"invalid volume name {v!r}")
        tag_args = " ".join(f"--tag {q(t)}" for t in base_tags + [volume_tag(v)])
        lines.append(f'echo "@@kowan phase backup {v}"')
        lines.append(f"r backup --json --host {q(host_label)} {tag_args} {q(DATA_ROOT + '/' + v)} 2>&1")
        lines.append(f'echo "@@kowan rc backup {v} $?"')
    flags = retention_flags(retention)
    if flags:
        lines.append('echo "@@kowan phase forget"')
        lines.append(
            f"r forget --json --prune --tag {q(job_tag(job_id))} --group-by host,paths "
            + " ".join(q(f) for f in flags) + " 2>&1"
        )
        lines.append('echo "@@kowan rc forget all $?"')
    lines.append('echo "@@kowan done"')
    return "\n".join(lines) + "\n"


def restore_script(snapshot_id: str, *, delete: bool) -> str:
    """Restore the single path of ``snapshot_id`` into /target."""
    if not re.match(r"^[0-9a-f]{8,64}$", snapshot_id or ""):
        raise ValueError("invalid snapshot id")
    extra = " --delete" if delete else ""
    return (
        _PRELUDE
        + f"""echo "@@kowan phase lookup"
P=$(r snapshots --no-lock --json {snapshot_id} 2>/tmp/lookup.err | sed -n 's/.*"paths":\\["\\([^"]*\\)".*/\\1/p')
if [ -z "$P" ]; then cat /tmp/lookup.err; echo "@@kowan rc lookup snapshot 1"; exit 22; fi
echo "@@kowan phase restore $P"
r restore --json "{snapshot_id}:$P" --target {TARGET_MOUNT}{extra} 2>&1
echo "@@kowan rc restore snapshot $?"
echo "@@kowan done"
"""
    )


def snapshots_script(volume: str = "") -> str:
    tag = f" --tag {q(volume_tag(volume))}" if volume else " --tag kowan"
    return (
        _PRELUDE
        + f"""r snapshots --no-lock --json{tag} 2>/tmp/snap.err
rc=$?
if [ "$rc" -ne 0 ]; then cat /tmp/snap.err; fi
echo "@@kowan rc snapshots list $rc"
"""
    )


def ls_script(snapshot_id: str, path: str) -> str:
    if not re.match(r"^[0-9a-f]{8,64}$", snapshot_id or ""):
        raise ValueError("invalid snapshot id")
    return (
        _PRELUDE
        + f"""r ls --no-lock --json {snapshot_id} {q(path)} 2>/tmp/ls.err
rc=$?
if [ "$rc" -ne 0 ]; then cat /tmp/ls.err; fi
echo "@@kowan rc ls list $rc"
"""
    )


def check_script() -> str:
    """Open (and init if missing) the repository; used by "test repository"."""
    return _PRELUDE + _ENSURE_REPO + 'echo "@@kowan done"\n'


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def strip_timestamps(lines: Iterable[str]) -> List[str]:
    """Drop the RFC3339 prefix Docker adds with ``timestamps=1``."""
    return [_TS_RE.sub("", l.rstrip("\r")) for l in lines]


def _json_obj(line: str) -> Optional[Any]:
    s = line.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


@dataclass
class Section:
    phase: str
    name: str = ""
    lines: List[str] = field(default_factory=list)
    rc: Optional[int] = None


@dataclass
class ParsedRun:
    sections: List[Section] = field(default_factory=list)
    initialized: bool = False
    done: bool = False

    def section(self, phase: str, name: str = "") -> Optional[Section]:
        for s in self.sections:
            if s.phase == phase and (not name or s.name == name):
                return s
        return None


def parse_markers(lines: Iterable[str]) -> ParsedRun:
    out = ParsedRun()
    cur = Section(phase="prelude")
    out.sections.append(cur)
    for line in lines:
        if line.startswith(MARK + " "):
            parts = line[len(MARK) + 1:].split()
            if not parts:
                continue
            word = parts[0]
            if word == "phase":
                cur = Section(phase=parts[1] if len(parts) > 1 else "", name=" ".join(parts[2:]))
                out.sections.append(cur)
            elif word == "rc" and len(parts) >= 4:
                phase, name, code = parts[1], parts[2], parts[3]
                target = cur if (cur.phase == phase) else (out.section(phase, name) or cur)
                try:
                    target.rc = int(code)
                except ValueError:
                    target.rc = -1
            elif word == "initialized":
                out.initialized = True
            elif word == "done":
                out.done = True
            continue
        cur.lines.append(line)
    return out


def backup_summary(lines: Iterable[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Find the ``message_type == "summary"`` object of ``restic backup --json``
    and collect ``error`` messages. Returns (summary|None, errors)."""
    summary = None
    errs: List[str] = []
    for line in lines:
        obj = _json_obj(line)
        if not isinstance(obj, dict):
            continue
        mt = obj.get("message_type")
        if mt == "summary":
            summary = obj
        elif mt == "error":
            e = obj.get("error")
            msg = e.get("message") if isinstance(e, dict) else (str(e) if e else "")
            item = obj.get("item", "")
            errs.append(f"{item}: {msg}" if item else msg)
    return summary, errs


def stats_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "snapshotId": summary.get("snapshot_id", ""),
        "filesNew": int(summary.get("files_new", 0) or 0),
        "filesChanged": int(summary.get("files_changed", 0) or 0),
        "filesUnmodified": int(summary.get("files_unmodified", 0) or 0),
        "filesTotal": int(summary.get("total_files_processed", 0) or 0),
        "bytesAdded": int(summary.get("data_added", 0) or 0),
        "bytesAddedPacked": int(summary.get("data_added_packed", 0) or 0),
        "bytesTotal": int(summary.get("total_bytes_processed", 0) or 0),
        "durationSec": float(summary.get("total_duration", 0) or 0),
    }


def restore_summary(lines: Iterable[str]) -> Optional[Dict[str, Any]]:
    for line in lines:
        obj = _json_obj(line)
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            return {
                "filesRestored": int(obj.get("files_restored", 0) or 0),
                "filesTotal": int(obj.get("total_files", 0) or 0),
                "bytesRestored": int(obj.get("bytes_restored", 0) or 0),
                "bytesTotal": int(obj.get("total_bytes", 0) or 0),
                "durationSec": float(obj.get("seconds_elapsed", 0) or 0),
            }
    return None


def parse_snapshots(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Parse ``restic snapshots --json`` (one JSON array line) into API dicts,
    newest first."""
    out: List[Dict[str, Any]] = []
    for line in lines:
        obj = _json_obj(line)
        if not isinstance(obj, list):
            continue
        for s in obj:
            if not isinstance(s, dict):
                continue
            tags = [str(t) for t in (s.get("tags") or [])]
            vol = next((t.split("=", 1)[1] for t in tags if t.startswith("kowan-volume=")), "")
            job = next((t.split("=", 1)[1] for t in tags if t.startswith("kowan-job=")), "")
            summ = s.get("summary") or {}
            out.append(
                {
                    "id": s.get("id", ""),
                    "shortId": s.get("short_id", "") or str(s.get("id", ""))[:8],
                    "time": s.get("time", ""),
                    "hostname": s.get("hostname", ""),
                    "paths": [str(p) for p in (s.get("paths") or [])],
                    "tags": [t for t in tags if not t.startswith(("kowan-volume=", "kowan-job=")) and t != "kowan"],
                    "volume": vol,
                    "jobId": job,
                    "sizeBytes": int(summ.get("total_bytes_processed", 0) or 0),
                    "fileCount": int(summ.get("total_files_processed", 0) or 0),
                }
            )
    out.sort(key=lambda s: s["time"], reverse=True)
    return out


def parse_ls(lines: Iterable[str], dir_path: str, limit: int = 5000) -> Tuple[List[Dict[str, Any]], bool]:
    """Parse ``restic ls --json <snap> <dir>`` into the direct children of
    ``dir_path``. Returns (entries, truncated)."""
    d = "/" + dir_path.strip("/")
    out: List[Dict[str, Any]] = []
    truncated = False
    for line in lines:
        obj = _json_obj(line)
        if not isinstance(obj, dict):
            continue
        if obj.get("struct_type", "node") != "node" or "path" not in obj:
            continue
        p = str(obj.get("path", ""))
        parent = p.rsplit("/", 1)[0] or "/"
        if parent != d or p == d:
            continue
        if len(out) >= limit:
            truncated = True
            break
        out.append(
            {
                "name": obj.get("name", p.rsplit("/", 1)[-1]),
                "path": p,
                "type": obj.get("type", ""),
                "isDir": obj.get("type") == "dir",
                "size": int(obj.get("size", 0) or 0),
                "mtime": obj.get("mtime", ""),
                "mode": obj.get("permissions", "") or "",
            }
        )
    out.sort(key=lambda e: (not e["isDir"], e["name"]))
    return out, truncated


def clean_log(lines: Iterable[str]) -> str:
    """The human log kept in run history: drop restic's periodic JSON status
    lines (progress spam) and the internal marker lines' noise."""
    keep: List[str] = []
    for line in lines:
        if line.startswith(MARK + " "):
            parts = line[len(MARK) + 1:].split()
            if parts and parts[0] == "phase":
                keep.append("── " + " ".join(parts[1:]))
            elif parts and parts[0] == "rc" and len(parts) >= 4:
                keep.append(f"   exit {parts[3]}")
            elif parts and parts[0] == "timeout":
                keep.append("── " + " ".join(parts))
            continue
        obj = _json_obj(line)
        if isinstance(obj, dict) and obj.get("message_type") == "status":
            continue
        keep.append(line)
    return "\n".join(keep)
