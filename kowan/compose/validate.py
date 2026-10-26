"""Compose validation orchestrator.

:func:`validate_text` is pure apart from the optional ``docker compose`` call:
structural validation (CLI, or the schema-lite fallback) + the lint rules,
given whatever host facts the caller collected.

:func:`validate_for_target` collects those facts from a running control plane:
the target host's containers (published ports and names, excluding the
deployment being validated so a redeploy does not conflict with itself),
whether an absolute bind path exists (only checkable for the local host when
the server itself is not containerised) and the names/fields of Kowan secrets.
Host queries are time-boxed; an unreachable host just skips host rules.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from .diagnostics import Diagnostic, has_errors, sort_diagnostics
from .lint import LintContext, SecretInfo, lint, mask_secret_tokens
from .positions import parse
from .structural import schema_lite, validate_cli, yaml_error

log = logging.getLogger("kowan.compose")

_HOST_TIMEOUT = float(os.environ.get("KOWAN_COMPOSE_HOST_TIMEOUT", "5"))

# Compose project / deployment labels (kept local to avoid importing the
# deployment service, which imports a lot).
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_DEPLOYMENT_ID_LABEL = "kowan.deployment.id"


@dataclass
class ValidationResult:
    diagnostics: List[Diagnostic] = field(default_factory=list)
    #: "docker" (docker compose config), "builtin" (schema-lite) or "none".
    engine: str = "none"
    #: which host-dependent checks ran.
    checks: Dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not has_errors(self.diagnostics)

    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "engine": self.engine,
            "checks": self.checks,
            "summary": {
                sev: sum(1 for d in self.diagnostics if d.severity == sev)
                for sev in ("error", "warning", "info")
            },
        }


# Docker messages that a lint rule reports better (with a position and fix).
_COVERED = (
    ("depends on undefined service", "depends-on-unknown"),
    ("undefined volume", "volume-undeclared"),
)


def _dedupe(structural: List[Diagnostic], linted: List[Diagnostic]) -> List[Diagnostic]:
    codes = {d.code for d in linted}
    out: List[Diagnostic] = []
    for d in structural:
        if d.code == "compose-config" and any(s in d.message and c in codes for s, c in _COVERED):
            continue
        out.append(d)
    seen: Set[Tuple[str, str, int]] = set()
    merged: List[Diagnostic] = []
    # Lint first: on a tie its diagnostic carries the fix.
    for d in linted + out:
        k = (d.code, d.path, d.line)
        if k in seen:
            continue
        seen.add(k)
        merged.append(d)
    return merged


async def validate_text(
    text: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    host_ports: Optional[Dict[Tuple[int, str], str]] = None,
    host_container_names: Optional[Set[str]] = None,
    path_exists: Optional[Callable[[str], bool]] = None,
    secrets: Optional[Dict[str, SecretInfo]] = None,
    structural: bool = True,
    use_cli: bool = True,
) -> ValidationResult:
    """Validate compose ``text``. Never raises for bad input."""
    res = ValidationResult()
    masked = mask_secret_tokens(text or "")
    parsed = parse(masked)
    if parsed.error is not None:
        res.diagnostics = [yaml_error(parsed)]
        res.engine = "builtin"
        return res

    struct: List[Diagnostic] = []
    if structural:
        cli = await validate_cli(masked, parsed, env) if use_cli else None
        if cli is not None:
            res.engine = "docker"
            struct = cli
        else:
            res.engine = "builtin"
            struct = schema_lite(parsed)

    ctx = LintContext(
        parsed=parsed,
        text=text or "",
        env=env,
        host_ports=host_ports,
        host_container_names=host_container_names,
        path_exists=path_exists,
        secrets=secrets,
    )
    linted = lint(ctx)
    res.diagnostics = sort_diagnostics(_dedupe(struct, linted))
    res.checks = {
        "hostPorts": host_ports is not None,
        "hostPaths": path_exists is not None,
        "secrets": secrets is not None,
    }
    return res


# ---------------------------------------------------------------------------
# fact collection against a live control plane
# ---------------------------------------------------------------------------


def secrets_from_store(store: Any) -> Optional[Dict[str, SecretInfo]]:
    """Kowan secrets by name (field names only), or None when unavailable."""
    try:
        creds = store.list_credentials("secret")
    except Exception:  # noqa: BLE001 - no cipher / store error
        return None
    try:
        from kowan import secrets as secrets_pkg

        is_external = secrets_pkg.is_external
    except Exception:  # noqa: BLE001
        def is_external(_t: str) -> bool:
            return False
    out: Dict[str, SecretInfo] = {}
    for c in creds:
        ext = bool(is_external(c.type)) or bool((c.meta or {}).get("connectionRef"))
        out[c.name] = SecretInfo(keys=set(c.config_keys or []), external=ext)
    return out


def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("KOWAN_IN_CONTAINER", "") == "1"


async def host_facts(
    svc: Any, host_id: str, *, deployment_id: str = "", project: str = ""
) -> Tuple[Optional[Dict[Tuple[int, str], str]], Optional[Set[str]], Optional[Callable[[str], bool]]]:
    """``(host_ports, container_names, path_exists)`` for ``host_id``; each
    ``None`` when it could not be determined."""
    if not host_id:
        return None, None, None
    try:
        host = svc.store.get_host(host_id)
    except Exception:  # noqa: BLE001 - unknown host
        return None, None, None
    path_exists = None
    if getattr(host, "type", "") == "local" and not _in_container():
        path_exists = os.path.exists
    try:
        ops = svc.docker.ops(host)
        containers = await asyncio.wait_for(ops.list_containers(True), timeout=_HOST_TIMEOUT)
    except Exception as err:  # noqa: BLE001 - offline host / timeout
        log.debug("compose validate: host %s unavailable: %s", host_id, err)
        return None, None, path_exists
    ports: Dict[Tuple[int, str], str] = {}
    names: Set[str] = set()
    for c in containers:
        labels = getattr(c, "labels", None) or {}
        if deployment_id and labels.get(_DEPLOYMENT_ID_LABEL) == deployment_id:
            continue
        if project and labels.get(_COMPOSE_PROJECT_LABEL) == project:
            continue
        cname = (c.names[0].lstrip("/") if getattr(c, "names", None) else "") or c.id[:12]
        names.add(cname)
        if getattr(c, "state", "") != "running":
            continue
        for p in getattr(c, "ports", None) or []:
            if getattr(p, "public_port", 0):
                ports.setdefault((int(p.public_port), str(p.type or "tcp")), cname)
    return ports, names, path_exists


async def validate_for_target(
    svc: Any,
    text: str,
    *,
    host_id: str = "",
    deployment_id: str = "",
    project: str = "",
    env: Optional[Mapping[str, str]] = None,
    structural: bool = True,
    host_checks: bool = True,
) -> ValidationResult:
    """Validate ``text`` for a deployment on ``host_id`` using live facts."""
    ports = names = path_exists = None
    if host_checks and host_id:
        ports, names, path_exists = await host_facts(
            svc, host_id, deployment_id=deployment_id, project=project
        )
    secrets = secrets_from_store(svc.store) if getattr(svc, "store", None) is not None else None
    return await validate_text(
        text,
        env=env,
        host_ports=ports,
        host_container_names=names,
        path_exists=path_exists,
        secrets=secrets,
        structural=structural,
    )
