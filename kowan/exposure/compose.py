"""Edit a compose file to expose / unexpose one service through a proxy.

The service gets the proxy labels (see :mod:`labels`) and joins the proxy's
network, declared as an external network under the reserved key
:data:`NET_KEY`. Re-serialising YAML drops comments; the API warns about it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml

from kowan import yamlsafe

from . import labels as lbl

NET_KEY = "kowan_proxy"


class ComposeEditError(ValueError):
    """The compose file cannot be edited as requested (maps to HTTP 400/409)."""


def _load(compose: str) -> Dict[str, Any]:
    try:
        doc = yamlsafe.safe_load(compose or "")
    except yaml.YAMLError as exc:
        raise ComposeEditError(f"compose file is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise ComposeEditError("compose file has no services")
    return doc


def services(compose: str) -> List[str]:
    """Service names of a compose file, in file order."""
    return [str(k) for k in _load(compose)["services"].keys()]


def service_labels(compose: str, service: str) -> Dict[str, str]:
    doc = _load(compose)
    svc = doc["services"].get(service)
    if not isinstance(svc, dict):
        raise ComposeEditError(f"service {service!r} not found")
    return _labels_dict(svc.get("labels"))


def _labels_dict(raw: Any) -> Dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out: Dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            s = str(item)
            k, _, v = s.partition("=")
            out[k] = v
    return out


def _dump(doc: Dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)


def expose(compose: str, service: str, labels: Dict[str, str], network: Optional[str]) -> str:
    """Return ``compose`` with ``labels`` merged into ``service`` and, when
    ``network`` is given, the service attached to that external network."""
    doc = _load(compose)
    svc = doc["services"].get(service)
    if not isinstance(svc, dict):
        raise ComposeEditError(f"service {service!r} not found")
    merged = _labels_dict(svc.get("labels"))
    merged.update(labels)
    svc["labels"] = merged
    if network:
        if svc.get("network_mode"):
            raise ComposeEditError(
                f"service {service!r} uses network_mode={svc['network_mode']!r}; it cannot join the proxy network"
            )
        nets = svc.get("networks")
        if nets is None:
            svc["networks"] = ["default", NET_KEY]
        elif isinstance(nets, list):
            if NET_KEY not in nets:
                nets.append(NET_KEY)
        elif isinstance(nets, dict):
            nets.setdefault(NET_KEY, None)
        else:
            raise ComposeEditError(f"service {service!r} has an unsupported networks section")
        top = doc.get("networks")
        if not isinstance(top, dict):
            top = {}
            doc["networks"] = top
        top[NET_KEY] = {"external": True, "name": network}
    return _dump(doc)


def unexpose(compose: str, service: str, remove_keys: List[str], drop_network: bool) -> str:
    """Return ``compose`` with ``remove_keys`` dropped from ``service`` labels and,
    when ``drop_network``, the proxy network detached (and removed from the
    top-level section once no service references it)."""
    doc = _load(compose)
    svc = doc["services"].get(service)
    if not isinstance(svc, dict):
        raise ComposeEditError(f"service {service!r} not found")
    merged = _labels_dict(svc.get("labels"))
    for k in remove_keys:
        merged.pop(k, None)
    if merged:
        svc["labels"] = merged
    else:
        svc.pop("labels", None)
    if drop_network:
        nets = svc.get("networks")
        if isinstance(nets, list) and NET_KEY in nets:
            nets.remove(NET_KEY)
            if nets == ["default"]:
                svc.pop("networks", None)
        elif isinstance(nets, dict):
            nets.pop(NET_KEY, None)
            if not nets:
                svc.pop("networks", None)
        still_used = False
        for other in doc["services"].values():
            if not isinstance(other, dict):
                continue
            n = other.get("networks")
            if (isinstance(n, list) and NET_KEY in n) or (isinstance(n, dict) and NET_KEY in n):
                still_used = True
        top = doc.get("networks")
        if isinstance(top, dict) and not still_used:
            top.pop(NET_KEY, None)
            if not top:
                doc.pop("networks", None)
    return _dump(doc)


def exposure_of(compose: str, service: str) -> Optional[Dict[str, str]]:
    return lbl.exposed_by_port(service_labels(compose, service))
