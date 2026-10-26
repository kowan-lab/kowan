"""Structural validation of a compose file.

Primary path: ``docker compose -f - config --quiet`` - the real Compose
loader/validator, shipped in the server image as the compose CLI plugin. It
needs no daemon for ``config``. It runs in an empty temporary project
directory with a minimal environment, so neither the server's cwd nor its
environment variables leak into the result.

Fallback (no CLI, or it cannot run): :func:`schema_lite`, a built-in check of
the parts of the Compose spec that matter most - top-level keys, service shape,
list/map types, undeclared named volumes/networks, duplicate keys.

Error messages from the CLI rarely carry positions; :func:`locate` maps the
common shapes (``L3.C5``, ``line 4``, ``services.web.ports``, ``service "web"``)
back to a line/column through the parsed node map.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from typing import Dict, List, Mapping, Optional, Tuple

from .diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Diagnostic,
    PathSeg,
    format_path,
)
from .positions import ParsedYaml

_TIMEOUT = float(os.environ.get("KOWAN_COMPOSE_VALIDATE_TIMEOUT", "10"))

# Cached "is `docker compose` usable here?" (None = not probed yet).
_cli_ok: Optional[bool] = None


def _cli_env(extra: Optional[Mapping[str, str]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for k in ("PATH", "HOME", "DOCKER_CONFIG", "TMPDIR", "SYSTEMROOT"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    env["COMPOSE_PROJECT_NAME"] = "kowan-validate"
    for k, v in (extra or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            env[k] = str(v)
    return env


async def _run_cli(text: str, env: Optional[Mapping[str, str]]) -> Tuple[int, str]:
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("docker CLI not found")
    tmp = tempfile.mkdtemp(prefix="kowan-compose-")
    try:
        proc = await asyncio.create_subprocess_exec(
            docker, "compose", "--project-directory", tmp, "-p", "kowan-validate",
            "-f", "-", "config", "--quiet",
            cwd=tmp,
            env=_cli_env(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(text.encode()), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def cli_available() -> bool:
    """Whether ``docker compose`` can run here (probed once, then cached)."""
    global _cli_ok
    if _cli_ok is None:
        try:
            rc, _ = await _run_cli("services: {}\n", None)
            _cli_ok = rc == 0
        except Exception:  # noqa: BLE001 - missing binary/plugin, timeout
            _cli_ok = False
    return _cli_ok


# ---------------------------------------------------------------------------
# message -> position
# ---------------------------------------------------------------------------

_RE_LC = re.compile(r"\bL(\d+)\.C(\d+)")
_RE_LINE = re.compile(r"\bline (\d+)")
_RE_DOTTED = re.compile(r"\b(services|volumes|networks|secrets|configs)((?:\.[A-Za-z0-9_.\-]+)+)")
_RE_SERVICE = re.compile(r'service "([^"]+)"')
_RE_PROP = re.compile(r"additional propert(?:y|ies) '([^']+)'")


def _resolve_dotted(parsed: ParsedYaml, root: str, rest: str) -> List[PathSeg]:
    """Turn ``services`` + ``.web.deploy.resources`` into the longest existing
    path. Service names may contain dots, so we greedily match known keys."""
    parts = rest.strip(".").split(".")
    path: List[PathSeg] = [root]
    i = 0
    while i < len(parts):
        matched = False
        # Try the longest dotted run first (a key "my.svc").
        for j in range(len(parts), i, -1):
            cand = ".".join(parts[i:j])
            if tuple(path + [cand]) in parsed.keys:
                path.append(cand)
                i = j
                matched = True
                break
        if not matched:
            break
    return path


def locate(message: str, parsed: ParsedYaml) -> Tuple[str, int, int]:
    """Best-effort ``(path, line, column)`` for a Compose error message."""
    m = _RE_LC.search(message)
    if m:
        return "", int(m.group(1)), int(m.group(2))
    path: List[PathSeg] = []
    m = _RE_DOTTED.search(message)
    if m:
        path = _resolve_dotted(parsed, m.group(1), m.group(2))
    else:
        m = _RE_SERVICE.search(message)
        if m and ("services", m.group(1)) in parsed.keys:
            path = ["services", m.group(1)]
    p = _RE_PROP.search(message)
    if p and path and tuple(path + [p.group(1)]) in parsed.keys:
        path = path + [p.group(1)]
    if path:
        line, col = parsed.pos(path)
        if line:
            return format_path(path), line, col
    lines = [int(x) for x in _RE_LINE.findall(message)]
    if lines:
        return "", max(lines), 1
    return "", 0, 0


def _clean(message: str) -> str:
    msg = message.strip()
    for prefix in ("validating -: ", "failed to parse -: ", "yaml: "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    msg = msg.replace(": invalid compose project", "")
    return msg


def _cli_diagnostics(rc: int, output: str, parsed: ParsedYaml) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "level=warning" in line:
            # Unset-variable warnings are produced by our own env-unset rule
            # (with positions); other warnings are surfaced as info.
            if "variable is not set" in line:
                continue
            m = re.search(r'msg="(.*)"', line)
            out.append(Diagnostic(SEVERITY_INFO, "compose-config", (m.group(1) if m else line).replace('\\"', '"')))
            continue
        if rc == 0:
            continue
        msg = _clean(line)
        path, ln, col = locate(line, parsed)
        if msg.startswith("env file ") and "not found" in msg:
            # env_file is read on the TARGET host's project dir, not here.
            out.append(Diagnostic(
                SEVERITY_INFO, "env-file-unchecked",
                "env_file is read on the target host and cannot be checked here; "
                "the rest of the file was not validated by docker compose",
                path or _first_key(parsed, "env_file"), *(_pos_of(parsed, "env_file") if not ln else (ln, col)),
            ))
            continue
        if "required variable" in msg:
            out.append(Diagnostic(SEVERITY_WARNING, "env-required", msg, path, ln, col))
            continue
        code = "yaml-syntax" if ("yaml" in line.lower() and ("line" in line or "L" in line)) else "compose-config"
        out.append(Diagnostic(SEVERITY_ERROR, code, msg, path, ln, col))
    if rc != 0 and not any(d.severity == SEVERITY_ERROR for d in out) and not any(
        d.code == "env-file-unchecked" for d in out
    ) and not any(d.code == "env-required" for d in out):
        out.append(Diagnostic(SEVERITY_ERROR, "compose-config", output.strip()[-400:] or f"docker compose config exited {rc}"))
    return out


def _first_key(parsed: ParsedYaml, key: str) -> str:
    for p in parsed.keys:
        if p and p[-1] == key:
            return format_path(p)
    return ""


def _pos_of(parsed: ParsedYaml, key: str) -> Tuple[int, int]:
    for p, pos in parsed.keys.items():
        if p and p[-1] == key:
            return pos
    return (0, 0)


async def validate_cli(text: str, parsed: ParsedYaml, env: Optional[Mapping[str, str]] = None) -> Optional[List[Diagnostic]]:
    """Run ``docker compose config``; ``None`` when the CLI is unavailable."""
    if not await cli_available():
        return None
    try:
        rc, output = await _run_cli(text, env)
    except Exception:  # noqa: BLE001 - timeout / vanished binary -> fallback
        return None
    return _cli_diagnostics(rc, output, parsed)


# ---------------------------------------------------------------------------
# schema-lite fallback
# ---------------------------------------------------------------------------

TOP_LEVEL_KEYS = {"version", "name", "services", "networks", "volumes", "configs", "secrets", "include", "models"}

SERVICE_KEYS = {
    "annotations", "attach", "blkio_config", "build", "cap_add", "cap_drop", "cgroup", "cgroup_parent",
    "command", "configs", "container_name", "cpu_count", "cpu_percent", "cpu_period", "cpu_quota",
    "cpu_rt_period", "cpu_rt_runtime", "cpu_shares", "cpus", "cpuset", "credential_spec", "depends_on",
    "deploy", "develop", "device_cgroup_rules", "devices", "dns", "dns_opt", "dns_search", "domainname",
    "entrypoint", "env_file", "environment", "expose", "extends", "external_links", "extra_hosts",
    "gpus", "group_add", "healthcheck", "hostname", "image", "init", "ipc", "isolation", "label_file", "labels",
    "links", "logging", "mac_address", "mem_limit", "mem_reservation", "mem_swappiness", "memswap_limit",
    "models", "network_mode", "networks", "oom_kill_disable", "oom_score_adj", "pid", "pids_limit", "platform",
    "ports", "post_start", "pre_stop", "privileged", "profiles", "provider", "pull_policy", "read_only",
    "restart", "runtime", "scale", "secrets", "security_opt", "shm_size", "stdin_open", "stop_grace_period",
    "stop_signal", "storage_opt", "sysctls", "tmpfs", "tty", "ulimits", "use_api_socket", "user", "userns_mode",
    "uts", "volumes", "volumes_from", "working_dir",
}

_LIST_KEYS = ("ports", "volumes", "cap_add", "cap_drop", "devices", "dns_search", "expose", "profiles", "security_opt")
_LIST_OR_MAP = ("environment", "labels", "depends_on", "networks", "extra_hosts", "sysctls")
_STR_OR_LIST = ("command", "entrypoint", "env_file", "dns", "tmpfs")


def named_volume_source(entry) -> Optional[str]:
    """The named-volume source of a service volume entry, or None for binds,
    anonymous volumes and tmpfs."""
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        src = parts[0]
    elif isinstance(entry, dict):
        if entry.get("type", "volume") != "volume":
            return None
        src = entry.get("source") or ""
    else:
        return None
    if not src or src.startswith(("/", ".", "~", "$")) or "/" in src or "\\" in src:
        return None
    return str(src)


def schema_lite(parsed: ParsedYaml) -> List[Diagnostic]:
    """Built-in structural check (no Docker needed)."""
    out: List[Diagnostic] = []

    def err(code: str, msg: str, path: List[PathSeg], sev: str = SEVERITY_ERROR) -> None:
        ln, col = parsed.pos(path)
        out.append(Diagnostic(sev, code, msg, format_path(path), ln, col))

    for path, (ln, col) in parsed.duplicates:
        out.append(Diagnostic(SEVERITY_ERROR, "schema", f"mapping key {path[-1]!r} is defined more than once",
                              format_path(list(path)), ln, col))
    doc = parsed.doc
    if doc is None:
        err("schema", "the compose file is empty", [])
        return out
    if not isinstance(doc, dict):
        err("schema", "the top level of a compose file must be a mapping", [])
        return out
    for k in doc:
        if str(k).startswith("x-"):
            continue
        if k not in TOP_LEVEL_KEYS:
            err("schema", f"additional property {k!r} is not allowed at the top level", [str(k)])
    services = doc.get("services")
    if services is None:
        if "include" not in doc:
            err("schema", "services is required", [])
        return out
    if not isinstance(services, dict):
        err("schema", "services must be a mapping", ["services"])
        return out
    declared_vols = doc.get("volumes") if isinstance(doc.get("volumes"), dict) else {}
    declared_nets = doc.get("networks") if isinstance(doc.get("networks"), dict) else {}
    for name, svc in services.items():
        sp: List[PathSeg] = ["services", str(name)]
        if not isinstance(svc, dict):
            err("schema", f"service {name!r} must be a mapping", sp)
            continue
        if "image" not in svc and "build" not in svc and "extends" not in svc and "provider" not in svc:
            err("schema", f"service {name!r} has neither an image nor a build context", sp)
        for k in svc:
            if str(k).startswith("x-"):
                continue
            if k not in SERVICE_KEYS:
                err("schema", f"additional property {k!r} is not allowed in service {name!r}", sp + [str(k)])
        for k in _LIST_KEYS:
            if k in svc and svc[k] is not None and not isinstance(svc[k], list):
                err("schema", f"services.{name}.{k} must be a list", sp + [k])
        for k in _LIST_OR_MAP:
            if k in svc and svc[k] is not None and not isinstance(svc[k], (list, dict)):
                err("schema", f"services.{name}.{k} must be a list or a mapping", sp + [k])
        for k in _STR_OR_LIST:
            if k in svc and svc[k] is not None and not isinstance(svc[k], (str, list)):
                err("schema", f"services.{name}.{k} must be a string or a list", sp + [k])
        if isinstance(svc.get("volumes"), list):
            for i, v in enumerate(svc["volumes"]):
                src = named_volume_source(v)
                if src is not None and src not in (declared_vols or {}):
                    err("volume-undeclared",
                        f"service {name!r} uses named volume {src!r}, which is not declared under the top-level volumes key",
                        sp + ["volumes", i])
        nets = svc.get("networks")
        names = list(nets.keys()) if isinstance(nets, dict) else (nets if isinstance(nets, list) else [])
        for i, n in enumerate(names):
            if isinstance(n, str) and n != "default" and n not in (declared_nets or {}):
                key: PathSeg = n if isinstance(nets, dict) else i
                err("schema", f"service {name!r} refers to undefined network {n!r}", sp + ["networks", key])
    return out


def yaml_error(parsed: ParsedYaml) -> Diagnostic:
    ln, col = parsed.error_pos
    return Diagnostic(SEVERITY_ERROR, "yaml-syntax", f"invalid YAML: {parsed.error}", "", ln, col)
