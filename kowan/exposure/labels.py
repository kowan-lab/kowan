"""Traefik label parsing and generation (Traefik v2/v3).

Parsing answers "which domains route to this container": Traefik routers
(``traefik.http.routers.<r>.rule=Host(`a`, `b`)``, ``HostSNI`` for TCP, and the
v1 ``traefik.frontend.rule=Host:a,b``).

Generation builds the label set the "Expose" action stamps on a container, and
records which keys it added (``kowan.exposure.keys``) so "Unexpose" removes
exactly those and nothing an operator wrote by hand.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

PROXY_TRAEFIK = "traefik"

# Marker labels written by Kowan (never by hand).
MARK_DOMAIN = "kowan.exposure.domain"
MARK_PROXY = "kowan.exposure.proxy"
MARK_ROUTER = "kowan.exposure.router"
MARK_NETWORK = "kowan.exposure.network"
MARK_KEYS = "kowan.exposure.keys"
MARKERS = (MARK_DOMAIN, MARK_PROXY, MARK_ROUTER, MARK_NETWORK, MARK_KEYS)

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$"
    r"|^localhost$"
)
_HOST_CALL_RE = re.compile(r"\b(Host|HostSNI|HostHeader)\s*\(([^)]*)\)")
_QUOTED_RE = re.compile(r"`([^`]*)`|\"([^\"]*)\"")
_ROUTER_KEY_RE = re.compile(r"^traefik\.(http|tcp|udp)\.routers\.([^.]+)\.(.+)$")


def valid_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.match((domain or "").strip().lower()))


def normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    if d.startswith("http://") or d.startswith("https://"):
        d = d.split("://", 1)[1]
    return d.split("/", 1)[0]


def parse_traefik_rule(rule: str) -> List[str]:
    """Hosts named by a Traefik rule, in order, de-duplicated."""
    out: List[str] = []
    for m in _HOST_CALL_RE.finditer(rule or ""):
        for q in _QUOTED_RE.finditer(m.group(2)):
            host = (q.group(1) if q.group(1) is not None else q.group(2) or "").strip().lower()
            if host and host != "*" and host not in out:
                out.append(host)
    return out


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def traefik_routes(labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """One entry per (router, host) declared in Traefik labels.

    Each: ``{"domain", "proxy": "traefik", "router", "protocol", "entrypoints",
    "tls", "certResolver", "rule", "enabled", "port"}``. ``enabled`` mirrors
    ``traefik.enable`` (absent counts as enabled; the proxy's
    ``exposedbydefault`` decides in that case).
    """
    labels = labels or {}
    routers: Dict[Tuple[str, str], Dict[str, str]] = {}
    for k, v in labels.items():
        m = _ROUTER_KEY_RE.match(k)
        if not m:
            continue
        routers.setdefault((m.group(1), m.group(2)), {})[m.group(3).lower()] = str(v)
    enabled = not ("traefik.enable" in labels and not _truthy(labels.get("traefik.enable")))
    # A single declared service port applies to every router of the container.
    ports = [v for k, v in labels.items()
             if re.match(r"^traefik\.http\.services\.[^.]+\.loadbalancer\.server\.port$", k)]
    port = 0
    if len(ports) == 1:
        try:
            port = int(ports[0])
        except ValueError:
            port = 0
    out: List[Dict[str, Any]] = []
    for (proto, name), attrs in sorted(routers.items()):
        rule = attrs.get("rule", "")
        tls = _truthy(attrs.get("tls")) or bool(attrs.get("tls.certresolver")) or proto == "tcp" and "hostsni" in rule.lower()
        for host in parse_traefik_rule(rule):
            out.append({
                "domain": host,
                "proxy": PROXY_TRAEFIK,
                "router": name,
                "protocol": proto,
                "entrypoints": [e.strip() for e in attrs.get("entrypoints", "").split(",") if e.strip()],
                "tls": bool(tls),
                "certResolver": attrs.get("tls.certresolver", ""),
                "rule": rule,
                "enabled": enabled,
                "port": port,
            })
    # Traefik v1 frontends.
    v1 = labels.get("traefik.frontend.rule", "")
    if v1.lower().startswith("host:"):
        for host in v1.split(":", 1)[1].split(","):
            host = host.strip().lower()
            if host:
                out.append({
                    "domain": host, "proxy": PROXY_TRAEFIK, "router": "frontend", "protocol": "http",
                    "entrypoints": [], "tls": False, "certResolver": "", "rule": v1,
                    "enabled": enabled, "port": int(labels.get("traefik.port", "0") or 0)
                    if str(labels.get("traefik.port", "0")).isdigit() else 0,
                })
    return out


def routes(labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """All proxy routes declared by a container's labels."""
    return traefik_routes(labels)


def router_name(base: str) -> str:
    """A Traefik-safe router/service name derived from a container/service name."""
    s = re.sub(r"[^a-z0-9-]+", "-", (base or "").lower()).strip("-")
    s = s if s.startswith("kowan-") else "kowan-" + s
    return s[:60].rstrip("-") or "kowan-app"


def expose_labels(
    proxy: str,
    *,
    domain: str,
    port: int,
    router: str,
    network: str = "",
    entrypoint: str = "",
    tls: bool = True,
    cert_resolver: str = "",
    network_added: bool = False,
) -> Dict[str, str]:
    """The label set that publishes a container on ``domain`` through ``proxy``.

    Includes the ``kowan.exposure.*`` markers; ``kowan.exposure.keys`` lists the
    proxy keys so :func:`unexpose_keys` can remove exactly what was added.
    """
    d = normalize_domain(domain)
    if not valid_domain(d):
        raise ValueError(f"invalid domain {domain!r}")
    if not (0 < int(port) < 65536):
        raise ValueError("port must be between 1 and 65535")
    keys: Dict[str, str] = {}
    if proxy == PROXY_TRAEFIK:
        keys["traefik.enable"] = "true"
        keys[f"traefik.http.routers.{router}.rule"] = f"Host(`{d}`)"
        if entrypoint:
            keys[f"traefik.http.routers.{router}.entrypoints"] = entrypoint
        if tls:
            keys[f"traefik.http.routers.{router}.tls"] = "true"
            if cert_resolver:
                keys[f"traefik.http.routers.{router}.tls.certresolver"] = cert_resolver
        keys[f"traefik.http.routers.{router}.service"] = router
        keys[f"traefik.http.services.{router}.loadbalancer.server.port"] = str(int(port))
        if network:
            keys["traefik.docker.network"] = network
    else:
        raise ValueError(f"unsupported proxy {proxy!r}")
    out = dict(keys)
    out[MARK_DOMAIN] = d
    out[MARK_PROXY] = proxy
    out[MARK_ROUTER] = router
    out[MARK_KEYS] = ",".join(sorted(keys))
    if network and network_added:
        out[MARK_NETWORK] = network
    return out


def exposed_by_port(labels: Dict[str, str]) -> Optional[Dict[str, str]]:
    """The exposure port recorded on a container, or None if not exposed by Kowan."""
    labels = labels or {}
    if MARK_DOMAIN not in labels:
        return None
    return {
        "domain": labels.get(MARK_DOMAIN, ""),
        "proxy": labels.get(MARK_PROXY, ""),
        "router": labels.get(MARK_ROUTER, ""),
        "network": labels.get(MARK_NETWORK, ""),
    }


def unexpose_keys(labels: Dict[str, str]) -> List[str]:
    """Label keys to remove to undo :func:`expose_labels` (markers included)."""
    labels = labels or {}
    keys = [k for k in (labels.get(MARK_KEYS, "") or "").split(",") if k]
    return sorted(set(keys) | {k for k in MARKERS if k in labels})
