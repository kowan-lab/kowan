"""Detect a reverse proxy running on a host and read its configuration.

Traefik is configured by CLI flags (``--entrypoints.websecure.address=:443``),
``TRAEFIK_*`` environment variables or a static file (``/etc/traefik/traefik.yml``
or ``--configFile``). The flag/env parsing here is pure; the file is read
through the container archive API by :func:`describe` (works on the stock
image, no shell needed).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import yaml

from kowan import yamlsafe

from . import labels as lbl

LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def _image_repo(image: str) -> str:
    ref = (image or "").lower().split("@", 1)[0]
    # strip tag (a ':' after the last '/')
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > last_slash:
        ref = ref[:colon]
    return ref


def proxy_kind(image: str) -> Optional[str]:
    """``traefik`` when ``image`` is a Traefik image, else None."""
    repo = _image_repo(image)
    last = repo.rsplit("/", 1)[-1]
    if last == "traefik":
        return lbl.PROXY_TRAEFIK
    return None


def _set_path(cfg: Dict[str, Any], dotted: str, value: str) -> None:
    cfg[dotted.lower()] = value


def flags_to_config(args: List[str], env: Optional[List[str]] = None) -> Dict[str, str]:
    """Flatten Traefik CLI flags and ``TRAEFIK_*`` env into ``{dotted.key: value}``
    (keys lower-cased; a bare ``--flag`` means ``true``)."""
    cfg: Dict[str, str] = {}
    for e in env or []:
        k, sep, v = str(e).partition("=")
        if not sep or not k.startswith("TRAEFIK_"):
            continue
        _set_path(cfg, k[len("TRAEFIK_"):].replace("_", "."), v)
    for a in args or []:
        a = str(a)
        if not a.startswith("--"):
            continue
        k, sep, v = a[2:].partition("=")
        _set_path(cfg, k, v if sep else "true")
    return cfg


def _file_to_config(doc: Any) -> Dict[str, str]:
    """Flatten a parsed static config file into the same dotted shape."""
    out: Dict[str, str] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict) and not node and prefix:
            out[prefix.lower()] = "true"
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        elif isinstance(node, list):
            out[prefix.lower()] = ",".join(str(x) for x in node)
        elif node is None:
            out[prefix.lower()] = "true"
        else:
            out[prefix.lower()] = str(node).lower() if isinstance(node, bool) else str(node)

    walk("", doc)
    return out


def traefik_settings(cfg: Dict[str, str]) -> Dict[str, Any]:
    """Interpret a flattened Traefik static config."""
    eps: Dict[str, str] = {}
    resolvers: Dict[str, Dict[str, Any]] = {}
    for k, v in cfg.items():
        m = re.match(r"^entrypoints\.([^.]+)\.address$", k)
        if m:
            eps[m.group(1)] = v
            continue
        m = re.match(r"^certificatesresolvers\.([^.]+)\.acme\.(.+)$", k)
        if m:
            r = resolvers.setdefault(m.group(1), {"storage": "acme.json", "email": "", "challenge": "", "caServer": ""})
            sub = m.group(2)
            if sub == "storage":
                r["storage"] = v
            elif sub == "email":
                r["email"] = v
            elif sub == "caserver":
                r["caServer"] = v
            elif sub.startswith("httpchallenge"):
                r["challenge"] = "http-01"
            elif sub.startswith("tlschallenge"):
                r["challenge"] = "tls-alpn-01"
            elif sub.startswith("dnschallenge"):
                r["challenge"] = "dns-01"
    for r in resolvers.values():
        r["staging"] = "staging" in (r.get("caServer") or "")
    secure = ""
    http = ""
    for name in sorted(eps):
        addr = eps[name]
        port = addr.rsplit(":", 1)[-1].split("/", 1)[0]
        if port == "443" and not secure:
            secure = name
        if port == "80" and not http:
            http = name
    if not secure:
        for cand in ("websecure", "https", "web-secure"):
            if cand in eps:
                secure = cand
                break
    if not http:
        for cand in ("web", "http"):
            if cand in eps:
                http = cand
                break
    api_insecure = cfg.get("api.insecure", "").lower() == "true"
    api_enabled = api_insecure or cfg.get("api", "").lower() == "true" or any(k.startswith("api.") for k in cfg)
    return {
        "entrypoints": eps,
        "secureEntrypoint": secure,
        "httpEntrypoint": http,
        "resolvers": resolvers,
        "certResolvers": sorted(resolvers),
        "dockerNetwork": cfg.get("providers.docker.network", ""),
        "dockerProvider": any(k == "providers.docker" or k.startswith("providers.docker.") for k in cfg),
        "exposedByDefault": cfg.get("providers.docker.exposedbydefault", "true").lower() != "false",
        "apiEnabled": api_enabled,
        "apiInsecure": api_insecure,
        "apiAddress": eps.get("traefik", ":8080"),
    }


def config_file_path(cfg: Dict[str, str]) -> str:
    return cfg.get("configfile", "")


def merge_file_config(flag_cfg: Dict[str, str], file_doc: Any) -> Dict[str, str]:
    """Static file settings overlaid by flags (Traefik itself refuses mixing, but
    an empty arg list plus a file is the common case)."""
    out = _file_to_config(file_doc) if file_doc else {}
    out.update(flag_cfg)
    return out


def parse_file(text: str) -> Any:
    try:
        return yamlsafe.safe_load(text or "")
    except yaml.YAMLError:
        return None


def pick_network(proxy: Dict[str, Any], target_networks: List[str]) -> Dict[str, Any]:
    """Choose the network the proxy reaches the target on.

    Returns ``{"network": name, "connect": bool}``: an explicit provider network
    (Traefik ``providers.docker.network``) wins; else a network
    the two already share; else the proxy's first user-defined network.
    """
    preferred = proxy.get("dockerNetwork") or ""
    proxy_nets = list(proxy.get("networks") or [])
    if preferred:
        return {"network": preferred, "connect": preferred not in target_networks}
    shared = [n for n in proxy_nets if n in target_networks]
    if shared:
        return {"network": shared[0], "connect": False}
    user = [n for n in proxy_nets if n not in ("bridge", "host", "none")]
    if user:
        return {"network": user[0], "connect": True}
    if proxy_nets:
        return {"network": proxy_nets[0], "connect": proxy_nets[0] not in target_networks}
    return {"network": "", "connect": False}


def traefik_compose(*, email: str, http_port: int = 80, https_port: int = 443,
                    network: str = "kowan-proxy", staging: bool = False,
                    image: str = "traefik:v3.6") -> str:
    """Compose file for a Traefik with Let's Encrypt (HTTP-01) and a docker provider.

    The dashboard API listens on 127.0.0.1:8080 inside the container only (Kowan
    reads router status through exec); HTTP redirects to HTTPS, except the ACME
    challenge which Traefik answers first.
    """
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
        raise ValueError("a valid email is required for Let's Encrypt")
    cmd = [
        "--providers.docker=true",
        "--providers.docker.exposedbydefault=false",
        f"--providers.docker.network={network}",
        "--entrypoints.web.address=:80",
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
        "--entrypoints.websecure.address=:443",
        "--entrypoints.traefik.address=127.0.0.1:8080",
        "--api.insecure=true",
        "--ping=true",
        f"--certificatesresolvers.letsencrypt.acme.email={email}",
        "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json",
        "--certificatesresolvers.letsencrypt.acme.httpchallenge=true",
        "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
    ]
    if staging:
        cmd.append(f"--certificatesresolvers.letsencrypt.acme.caserver={LE_STAGING}")
    doc = {
        "services": {
            "traefik": {
                "image": image,
                "restart": "unless-stopped",
                "command": cmd,
                "ports": [f"{int(http_port)}:80", f"{int(https_port)}:443"],
                "volumes": [
                    "/var/run/docker.sock:/var/run/docker.sock:ro",
                    "letsencrypt:/letsencrypt",
                ],
                "networks": ["proxy"],
                "labels": {"kowan.exposure.role": "proxy"},
            }
        },
        "networks": {"proxy": {"name": network}},
        "volumes": {"letsencrypt": {}},
    }
    # "$" in an email would be interpolated by compose; escape it.
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False).replace("$", "$$")
