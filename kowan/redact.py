"""Hide secret values from readers who may see a resource but not its secrets.

Viewers read containers and deployments; environment values are where
passwords live (``POSTGRES_PASSWORD=…``), so a viewer gets the names with the
values masked. The MCP tools already work this way; the REST API now matches.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import yaml

from kowan import yamlsafe

MASK = "********"


def mask_env_list(env: Any) -> Any:
    """``["KEY=value", …]`` (Docker inspect) -> ``["KEY=********", …]``."""
    if not isinstance(env, list):
        return env
    out: List[Any] = []
    for item in env:
        if isinstance(item, str):
            key, sep, _ = item.partition("=")
            out.append(f"{key}={MASK}" if sep else item)
        else:
            out.append(item)
    return out


def redact_inspect(info: Any) -> Any:
    """Mask ``Config.Env`` in a container inspect document (in place)."""
    if isinstance(info, dict) and isinstance(info.get("Config"), dict):
        info["Config"]["Env"] = mask_env_list(info["Config"].get("Env"))
    return info


def _env_names(env: Any) -> Dict[str, str]:
    if isinstance(env, dict):
        return {str(k): MASK for k in env}
    if isinstance(env, list):
        return {str(item).partition("=")[0]: MASK for item in env if isinstance(item, (str, int))}
    return {}


# What a reader may see of a service. Anything else can carry a secret: YAML
# anchors and x- fields, build args, inline secrets/configs, commands,
# healthchecks (mysqladmin -p…), labels (proxy basic-auth hashes).
_SERVICE_KEYS = ("image", "ports", "expose", "depends_on", "restart", "networks", "volumes")


def redact_compose(text: str) -> str:
    """A reading copy of a compose file that cannot leak secrets.

    Built from an allowlist rather than by masking known places: services keep
    their image, ports, dependencies, restart policy, networks and volumes, and
    environment variable names with the values hidden. Everything else is left
    out. If the file does not parse, nothing of it is returned.
    """
    if not text:
        return text
    try:
        doc = yamlsafe.safe_load(text)
    except yaml.YAMLError:
        return "# compose hidden: it could not be parsed\n"
    if not isinstance(doc, dict):
        return "# compose hidden: unexpected format\n"
    out: Dict[str, Any] = {"services": {}}
    services = doc.get("services")
    if isinstance(services, dict):
        for name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            view = {k: svc[k] for k in _SERVICE_KEYS if k in svc}
            env = _env_names(svc.get("environment"))
            if env:
                view["environment"] = env
            out["services"][str(name)] = view
    for key in ("networks", "volumes"):
        if isinstance(doc.get(key), dict):
            out[key] = {str(n): None for n in doc[key]}
    header = "# Read-only view: values that may hold secrets are hidden.\n"
    # Dumping plain dicts (no shared nodes) also defuses YAML alias bombs.
    return header + yaml.safe_dump(json.loads(json.dumps(out, default=str)), sort_keys=False, default_flow_style=False)


def strip_userinfo(url: str) -> str:
    """``https://user:token@host/x`` -> ``https://host/x``."""
    if not url or "@" not in url or "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    return f"{scheme}://{authority}{slash}{path}"
