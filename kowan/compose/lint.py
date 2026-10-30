"""Kowan's compose lint rules.

Each rule is a small function ``rule(ctx) -> list[Diagnostic]`` registered in
:data:`RULES`. Rules only read the parsed document (plus optional host facts in
the :class:`LintContext`); a rule that needs a fact the caller could not
provide (host containers, filesystem, secrets) simply skips.

Codes (stable, documented in docs/compose-lint.md):

=====================  ========  ==============================================
code                   severity  meaning
=====================  ========  ==============================================
image-no-tag           warning   image has no tag (implicitly ``:latest``)
image-latest           warning   image pinned to ``:latest``
restart-missing        info      no restart policy (fix: ``unless-stopped``)
privileged             warning   ``privileged: true``
network-host           warning   ``network_mode: host``
pid-host               warning   ``pid: host``
docker-socket          warning   the Docker socket is mounted
port-in-use            error     published port already used on the host
port-duplicate         error     two services publish the same host port
bind-path-missing      warn/err  absolute bind source missing on the host
volume-undeclared      error     named volume not declared at the top level
healthcheck-missing    info      a dependency has no healthcheck
healthcheck-required   warning   ``condition: service_healthy`` w/o healthcheck
depends-on-unknown     error     depends_on names an unknown service
container-name-dup     error     two services share a container_name
container-name-in-use  error     container_name already taken on the host
env-unset              warning   ``${VAR}`` without default and not set
secret-missing         error     ``[[name]]`` refers to no Kowan secret
secret-field-missing   error     ``[[name.field]]`` field not in the secret
secret-field-ambiguous error     bare ``[[name]]`` on a multi-field secret
=====================  ========  ==============================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple

from .diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Diagnostic,
    Fix,
    PathSeg,
    format_path,
)
from .positions import ParsedYaml
from .structural import named_volume_source

DEFAULT_RESTART = "unless-stopped"
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")


@dataclass
class SecretInfo:
    keys: Set[str] = field(default_factory=set)
    external: bool = False  # fields are resolved live by an external backend


@dataclass
class LintContext:
    parsed: ParsedYaml
    text: str
    #: variables known to be set (e.g. a git deployment's .env); None = none.
    env: Optional[Mapping[str, str]] = None
    #: published ports in use on the target host: (port, proto) -> container.
    host_ports: Optional[Dict[Tuple[int, str], str]] = None
    #: container names in use on the target host (excluding this deployment).
    host_container_names: Optional[Set[str]] = None
    #: absolute-path existence check on the target host; None = not checkable.
    path_exists: Optional[Callable[[str], bool]] = None
    #: Kowan secrets by name; None = unknown (rule skipped).
    secrets: Optional[Dict[str, SecretInfo]] = None

    @property
    def services(self) -> Dict[str, dict]:
        doc = self.parsed.doc
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            return {}
        return {str(k): v for k, v in doc["services"].items() if isinstance(v, dict)}


def _diag(ctx: LintContext, sev: str, code: str, msg: str, path: List[PathSeg],
          fix: Optional[Fix] = None, value: bool = False) -> Diagnostic:
    ln, col = ctx.parsed.value_pos(path) if value else ctx.parsed.pos(path)
    return Diagnostic(sev, code, msg, format_path(path), ln, col, fix)


# ---------------------------------------------------------------------------
# images / restart / security
# ---------------------------------------------------------------------------


def image_tag(image: str) -> Tuple[str, bool]:
    """``(tag, has_digest)`` of an image reference; tag "" when absent."""
    if "@" in image:
        return "", True
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        return last.split(":", 1)[1], False
    return "", False


def rule_image(ctx: LintContext) -> List[Diagnostic]:
    out = []
    for name, svc in ctx.services.items():
        image = svc.get("image")
        if not isinstance(image, str) or not image.strip() or "$" in image:
            continue
        tag, digest = image_tag(image.strip())
        path: List[PathSeg] = ["services", name, "image"]
        if digest:
            continue
        if tag == "":
            out.append(_diag(ctx, SEVERITY_WARNING, "image-no-tag",
                             f"service {name!r}: image {image!r} has no tag, so it means :latest and changes under you; pin a version",
                             path, value=True))
        elif tag == "latest":
            out.append(_diag(ctx, SEVERITY_WARNING, "image-latest",
                             f"service {name!r}: image {image!r} uses :latest; pin a specific version for repeatable deploys",
                             path, value=True))
    return out


def rule_restart(ctx: LintContext) -> List[Diagnostic]:
    out = []
    for name, svc in ctx.services.items():
        if "restart" in svc:
            continue
        deploy = svc.get("deploy")
        if isinstance(deploy, dict) and "restart_policy" in deploy:
            continue
        path: List[PathSeg] = ["services", name, "restart"]
        out.append(_diag(
            ctx, SEVERITY_INFO, "restart-missing",
            f"service {name!r} has no restart policy: it stays down after a crash or a host reboot",
            ["services", name],
            Fix(f"Add restart: {DEFAULT_RESTART}", [{"op": "set", "path": path, "value": DEFAULT_RESTART}]),
        ))
    return out


def _volume_source(entry) -> Tuple[str, str]:
    """``(type, source)`` of a service volume entry (short or long syntax)."""
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) < 2:
            return "anonymous", ""
        src = parts[0]
        if src.startswith(("/", ".", "~")):
            return "bind", src
        return "volume", src
    if isinstance(entry, dict):
        return str(entry.get("type") or "volume"), str(entry.get("source") or "")
    return "", ""


def rule_security(ctx: LintContext) -> List[Diagnostic]:
    out = []
    for name, svc in ctx.services.items():
        if svc.get("privileged") is True:
            out.append(_diag(ctx, SEVERITY_WARNING, "privileged",
                             f"service {name!r} runs privileged: it has full access to the host's devices and kernel",
                             ["services", name, "privileged"]))
        if svc.get("network_mode") == "host":
            out.append(_diag(ctx, SEVERITY_WARNING, "network-host",
                             f"service {name!r} uses the host network: no isolation, and its ports bypass Docker's mapping",
                             ["services", name, "network_mode"]))
        if svc.get("pid") == "host":
            out.append(_diag(ctx, SEVERITY_WARNING, "pid-host",
                             f"service {name!r} shares the host PID namespace: it can see and signal every host process",
                             ["services", name, "pid"]))
        vols = svc.get("volumes")
        if isinstance(vols, list):
            for i, v in enumerate(vols):
                _, src = _volume_source(v)
                if src in DOCKER_SOCKETS or src.endswith("/docker.sock"):
                    out.append(_diag(ctx, SEVERITY_WARNING, "docker-socket",
                                     f"service {name!r} mounts the Docker socket: that is root on the host; mount it read-only through a proxy if you can",
                                     ["services", name, "volumes", i]))
    return out


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------

_SHORT_PORT = re.compile(
    r"^(?:(?P<ip>\[[^\]]+\]|\d{1,3}(?:\.\d{1,3}){3}):)?(?:(?P<pub>\d+(?:-\d+)?):)?(?P<target>\d+(?:-\d+)?)(?:/(?P<proto>tcp|udp|sctp))?$"
)


def _range(s: str) -> List[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        lo, hi = int(a), int(b)
        if hi < lo or hi - lo > 1000:
            return []
        return list(range(lo, hi + 1))
    return [int(s)]


def published_ports(entry) -> List[Tuple[int, str]]:
    """Host ports a ``ports`` entry publishes, as ``(port, proto)``."""
    if isinstance(entry, int):
        return []
    if isinstance(entry, str):
        if "$" in entry:
            return []
        m = _SHORT_PORT.match(entry.strip())
        if not m or not m.group("pub"):
            return []
        return [(p, m.group("proto") or "tcp") for p in _range(m.group("pub"))]
    if isinstance(entry, dict):
        pub = entry.get("published")
        if pub in (None, ""):
            return []
        pub = str(pub)
        if "$" in pub or not re.fullmatch(r"\d+(-\d+)?", pub):
            return []
        return [(p, str(entry.get("protocol") or "tcp")) for p in _range(pub)]
    return []


def _replace_published(entry, new_port: int):
    if isinstance(entry, str):
        m = _SHORT_PORT.match(entry.strip())
        if not m or not m.group("pub") or "-" in m.group("pub"):
            return None
        ip = (m.group("ip") + ":") if m.group("ip") else ""
        proto = ("/" + m.group("proto")) if m.group("proto") else ""
        return f"{ip}{new_port}:{m.group('target')}{proto}"
    return None


def rule_ports(ctx: LintContext) -> List[Diagnostic]:
    out = []
    used_here: Dict[Tuple[int, str], str] = {}
    all_pub: Set[int] = set()
    for svc in ctx.services.values():
        for e in svc.get("ports") or [] if isinstance(svc.get("ports"), list) else []:
            all_pub.update(p for p, _ in published_ports(e))
    host = ctx.host_ports or {}
    taken = all_pub | {p for p, _ in host}
    for name, svc in ctx.services.items():
        ports = svc.get("ports")
        if not isinstance(ports, list):
            continue
        for i, e in enumerate(ports):
            path: List[PathSeg] = ["services", name, "ports", i]
            for port, proto in published_ports(e):
                key = (port, proto)
                if key in used_here and used_here[key] != name:
                    out.append(_diag(ctx, SEVERITY_ERROR, "port-duplicate",
                                     f"host port {port}/{proto} is published by both {used_here[key]!r} and {name!r}",
                                     path))
                used_here.setdefault(key, name)
                if ctx.host_ports is not None and key in host:
                    fix = None
                    free = next((p for p in range(port + 1, min(port + 200, 65536)) if p not in taken), None)
                    if free is not None:
                        if isinstance(e, dict):
                            fix = Fix(f"Publish on {free} instead",
                                      [{"op": "set", "path": path + ["published"], "value": str(free) if isinstance(e.get('published'), str) else free}])
                        else:
                            repl = _replace_published(e, free)
                            if repl is not None:
                                fix = Fix(f"Publish on {free} instead", [{"op": "set", "path": path, "value": repl}])
                        if fix is not None:
                            taken.add(free)
                    out.append(_diag(ctx, SEVERITY_ERROR, "port-in-use",
                                     f"host port {port}/{proto} is already used on the target host by {host[key]!r}",
                                     path, fix))
    return out


# ---------------------------------------------------------------------------
# volumes
# ---------------------------------------------------------------------------


def rule_volumes(ctx: LintContext) -> List[Diagnostic]:
    out = []
    doc = ctx.parsed.doc if isinstance(ctx.parsed.doc, dict) else {}
    declared = doc.get("volumes") if isinstance(doc.get("volumes"), dict) else {}
    for name, svc in ctx.services.items():
        vols = svc.get("volumes")
        if not isinstance(vols, list):
            continue
        for i, v in enumerate(vols):
            path: List[PathSeg] = ["services", name, "volumes", i]
            src = named_volume_source(v)
            if src is not None and src not in (declared or {}):
                out.append(_diag(
                    ctx, SEVERITY_ERROR, "volume-undeclared",
                    f"service {name!r} uses named volume {src!r}, which is not declared under the top-level volumes key",
                    path,
                    Fix(f"Declare volume {src!r}", [{"op": "set", "path": ["volumes", src], "value": {}}]),
                ))
                continue
            typ, bsrc = _volume_source(v)
            if typ != "bind" or not bsrc.startswith("/") or "$" in bsrc or ctx.path_exists is None:
                continue
            if bsrc in DOCKER_SOCKETS:
                continue
            try:
                exists = ctx.path_exists(bsrc)
            except Exception:  # noqa: BLE001 - not checkable after all
                continue
            if exists:
                continue
            if isinstance(v, dict):
                out.append(_diag(ctx, SEVERITY_ERROR, "bind-path-missing",
                                 f"bind source {bsrc!r} does not exist on the target host; long-syntax binds fail instead of creating it",
                                 path))
            else:
                out.append(_diag(ctx, SEVERITY_WARNING, "bind-path-missing",
                                 f"bind source {bsrc!r} does not exist on the target host; Docker will create it as an empty root-owned directory. Use a named volume or create the path first",
                                 path))
    return out


# ---------------------------------------------------------------------------
# dependencies / names
# ---------------------------------------------------------------------------


def _depends(svc: dict) -> List[Tuple[PathSeg, str, str]]:
    """``(path segment, target, condition)`` for each depends_on entry."""
    dep = svc.get("depends_on")
    out: List[Tuple[PathSeg, str, str]] = []
    if isinstance(dep, list):
        for i, t in enumerate(dep):
            if isinstance(t, str):
                out.append((i, t, ""))
    elif isinstance(dep, dict):
        for t, cfg in dep.items():
            cond = str(cfg.get("condition") or "") if isinstance(cfg, dict) else ""
            out.append((str(t), str(t), cond))
    return out


def _has_healthcheck(svc: dict) -> bool:
    hc = svc.get("healthcheck")
    if not isinstance(hc, dict):
        return False
    if hc.get("disable") is True:
        return False
    return bool(hc.get("test"))


def rule_depends(ctx: LintContext) -> List[Diagnostic]:
    out = []
    services = ctx.services
    reported: Set[str] = set()
    for name, svc in services.items():
        for seg, target, cond in _depends(svc):
            path: List[PathSeg] = ["services", name, "depends_on", seg]
            if target not in services:
                out.append(_diag(
                    ctx, SEVERITY_ERROR, "depends-on-unknown",
                    f"service {name!r} depends on {target!r}, which is not a service in this file",
                    path,
                    Fix(f"Remove {target!r} from depends_on", [{"op": "remove", "path": path}]),
                ))
                continue
            if _has_healthcheck(services[target]):
                continue
            if cond == "service_healthy":
                out.append(_diag(ctx, SEVERITY_WARNING, "healthcheck-required",
                                 f"service {name!r} waits for {target!r} to be healthy, but {target!r} defines no healthcheck (the image must provide one, or the deploy fails)",
                                 path))
            elif target not in reported:
                reported.add(target)
                out.append(_diag(ctx, SEVERITY_INFO, "healthcheck-missing",
                                 f"service {target!r} is a dependency of {name!r} but has no healthcheck: depends_on only waits for it to start, not to be ready",
                                 ["services", target]))
    return out


def rule_container_names(ctx: LintContext) -> List[Diagnostic]:
    out = []
    first: Dict[str, str] = {}
    for name, svc in ctx.services.items():
        cn = svc.get("container_name")
        if not isinstance(cn, str) or not cn or "$" in cn:
            continue
        path: List[PathSeg] = ["services", name, "container_name"]
        if cn in first:
            out.append(_diag(
                ctx, SEVERITY_ERROR, "container-name-dup",
                f"services {first[cn]!r} and {name!r} both use container_name {cn!r}; container names must be unique",
                path,
                Fix(f"Remove container_name from {name!r}", [{"op": "remove", "path": path}]),
            ))
            continue
        first[cn] = name
        if ctx.host_container_names is not None and cn in ctx.host_container_names:
            out.append(_diag(ctx, SEVERITY_ERROR, "container-name-in-use",
                             f"container name {cn!r} is already used by another container on the target host",
                             path, Fix(f"Remove container_name from {name!r}", [{"op": "remove", "path": path}])))
    return out


# ---------------------------------------------------------------------------
# text-level rules: env interpolation and Kowan secrets
# ---------------------------------------------------------------------------

_VAR = re.compile(
    r"\$(?:(?P<esc>\$)|\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?P<op>:?[-?+=][^}]*)?\}|(?P<named>[A-Za-z_][A-Za-z0-9_]*))"
)
SECRET_TOKEN = re.compile(r"\[\[\s*([A-Za-z0-9_.:-]+?)\s*\]\]")


def _comment_start(line: str) -> int:
    """Index where a YAML comment starts on ``line`` (len when none). Good
    enough for compose: a '#' at line start or after whitespace, outside quotes."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return i
    return len(line)


def _scan(text: str, pattern: "re.Pattern[str]"):
    """``finditer`` over the non-comment part of each line, yielding
    ``(match, line, column)``."""
    offset = 0
    for ln, line in enumerate(text.split("\n"), start=1):
        body = line[: _comment_start(line)]
        for m in pattern.finditer(body):
            yield m, ln, m.start() + 1
        offset += len(line) + 1


def _path_at_line(ctx: LintContext, line: int) -> str:
    best: Tuple[PathSeg, ...] = ()
    for p, (ln, _) in ctx.parsed.keys.items():
        if ln == line and len(p) > len(best):
            best = p
    return format_path(list(best))


def rule_env(ctx: LintContext) -> List[Diagnostic]:
    out = []
    known = ctx.env or {}
    seen: Set[Tuple[str, int]] = set()
    for m, ln, col in _scan(ctx.text, _VAR):
        if m.group("esc"):
            continue
        name = m.group("braced") or m.group("named")
        op = m.group("op") or ""
        if op[:2] in (":-", ":?", ":+") or op[:1] in ("-", "?", "+", "="):
            continue  # has a default / error message / alternate value
        if name in known or (name, ln) in seen:
            continue
        seen.add((name, ln))
        out.append(Diagnostic(
            SEVERITY_WARNING, "env-unset",
            f"${{{name}}} has no default and is not set; it becomes an empty string unless the target host's environment defines it. Use ${{{name}:-value}} or set it in .env",
            _path_at_line(ctx, ln), ln, col,
        ))
    return out


def rule_secrets(ctx: LintContext) -> List[Diagnostic]:
    if ctx.secrets is None:
        return []
    out = []
    for m, ln, col in _scan(ctx.text, SECRET_TOKEN):
        expr = m.group(1)
        source = ""
        rest = expr
        if "::" in expr:
            source, rest = expr.split("::", 1)
            source = source.strip().lower()
        if source not in ("", "secret"):
            continue  # plugin:: refs are resolved by plugins at deploy time
        name, sep, fld = rest.partition(".")
        info = ctx.secrets.get(name)
        path = _path_at_line(ctx, ln)
        if info is None:
            out.append(Diagnostic(SEVERITY_ERROR, "secret-missing",
                                  f"secret reference [[{expr}]] does not match any Kowan secret named {name!r}",
                                  path, ln, col))
        elif sep and fld and not info.external and fld not in info.keys:
            out.append(Diagnostic(SEVERITY_ERROR, "secret-field-missing",
                                  f"secret {name!r} has no field {fld!r} (fields: {', '.join(sorted(info.keys)) or 'none'})",
                                  path, ln, col))
        elif not sep and not info.external and len(info.keys) > 1 and "value" not in info.keys:
            out.append(Diagnostic(SEVERITY_ERROR, "secret-field-ambiguous",
                                  f"secret {name!r} has several fields; reference one as [[{name}.field]]",
                                  path, ln, col))
    return out


RULES: List[Callable[[LintContext], List[Diagnostic]]] = [
    rule_image,
    rule_restart,
    rule_security,
    rule_ports,
    rule_volumes,
    rule_depends,
    rule_container_names,
    rule_env,
    rule_secrets,
]


def lint(ctx: LintContext) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    if not ctx.services:
        return out
    for rule in RULES:
        out.extend(rule(ctx))
    return out


def mask_secret_tokens(text: str) -> str:
    """Replace each ``[[ref]]`` with same-length filler so YAML stays valid
    (``[[a]]`` would parse as a nested list) and positions do not move."""
    return SECRET_TOKEN.sub(lambda m: "x" * len(m.group(0)), text)
