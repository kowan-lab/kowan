"""Platform features an admin can turn off (Settings → Features).

Each feature lists the request paths it owns. While it is off those answer
404 with a message saying so, its public webhooks included, and the UI hides
it; GitOps also stops its periodic sync. Everything is on by default; the
state is one JSON setting.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

from aiohttp import web

_KEY = "features"

# id -> (label, paths it owns, as regular expressions on the request path)
FEATURES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "templates": ("Templates", (r"^/api/v1/templates(/|$)",)),
    "networkMap": ("Network map", (r"^/api/v1/netmap$", r"^/api/v1/hosts/[^/]+/netmap$")),
    "gitops": ("GitOps", (r"^/api/v1/gitops(/|$)", r"^/hooks/gitops/")),
}
_PATHS = {fid: tuple(re.compile(p) for p in paths) for fid, (_, paths) in FEATURES.items()}


def get(store: Any) -> Dict[str, bool]:
    """``{feature id: enabled}`` for every feature (on unless turned off)."""
    try:
        saved = json.loads(store.get_setting(_KEY) or "{}")
    except (ValueError, TypeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {fid: saved.get(fid) is not False for fid in FEATURES}


def enabled(store: Any, feature: str) -> bool:
    return get(store).get(feature, True)


def update(store: Any, changes: Dict[str, Any]) -> Dict[str, bool]:
    """Apply ``{feature id: bool}``; raises ValueError for anything else."""
    current = get(store)
    for fid, on in changes.items():
        if fid not in FEATURES:
            raise ValueError(f"unknown feature {fid!r}")
        if not isinstance(on, bool):
            raise ValueError(f"{fid} must be true or false")
        current[fid] = on
    store.set_setting(_KEY, json.dumps(current, sort_keys=True))
    return current


def owner(path: str) -> str:
    """The feature that owns a request path ("" for none)."""
    for fid, patterns in _PATHS.items():
        if any(p.search(path) for p in patterns):
            return fid
    return ""


def middleware(store: Any):
    """Answer 404 on the paths of a feature that is turned off."""

    @web.middleware
    async def mw(request: web.Request, handler):
        fid = owner(request.path)
        if fid and not enabled(store, fid):
            return web.json_response({"error": f"{FEATURES[fid][0]} is turned off in Settings"}, status=404)
        return await handler(request)

    return mw
