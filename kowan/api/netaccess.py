"""Settings > Network access: address allowlists managed from the UI.

The environment (KOWAN_UI_ALLOW_CIDRS, KOWAN_EDGE_ALLOW_CIDRS) stays the outer
limit and is shown read-only; the lists saved here can only narrow it (see
kowan.web.netpolicy). Saving is guarded three ways:

* admins only, and a local account must re-enter its password, so a stolen
  session alone cannot change who may connect;
* a UI list that would exclude the address you are connecting from is refused,
  so nobody locks themselves out;
* the change is recorded in the audit log like every other mutation.

Trusted proxies and the proxy header are environment-only: changing them from a
browser would let a caller decide which address the server believes.
"""

from __future__ import annotations

from typing import Any, Dict, List

from aiohttp import web

from kowan.auth import require_role, user_from, verify_password
from kowan.web import api_route, error_response, json_response, read_json
from kowan.web.httpx import reject_unknown_fields
from kowan.web.netpolicy import EDGE_SETTING, UI_SETTING, NetPolicy, parse_cidrs

MAX_ENTRIES = 100


def register(app: web.Application, svc: Any) -> None:
    api_route(app, "GET", "/settings/network", require_role("admin")(_get))
    api_route(app, "PUT", "/settings/network", require_role("admin")(_put))


def _svc(request: web.Request) -> Any:
    from kowan.web import SERVICES_KEY

    return request.app[SERVICES_KEY]


def _strs(nets) -> List[str]:
    """CIDRs as people write them: a single address without its /32 or /128."""
    return [str(n.network_address) if n.prefixlen == n.max_prefixlen else str(n) for n in nets]


def _view(request: web.Request, policy: NetPolicy) -> Dict[str, Any]:
    addr = policy.client_address(request)
    return {
        "env": {
            "uiAllow": _strs(policy.ui_allow),
            "edgeAllow": _strs(policy.agent_allow),
            "trustedProxies": _strs(policy.trusted_proxies),
            "proxyHeader": policy.proxy_header,
        },
        "uiAllow": _strs(policy.ui_allow_stored),
        "edgeAllow": _strs(policy.agent_allow_stored),
        "yourAddress": str(addr) if addr is not None else "",
        "peerAddress": request.remote or "",
    }


async def _get(request: web.Request) -> web.Response:
    return json_response(_view(request, _svc(request).netpolicy))


def _parse_list(body: Dict[str, Any], key: str, label: str):
    raw = body.get(key) or []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{key} must be a list of addresses or CIDRs")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"{label}: at most {MAX_ENTRIES} entries")
    return parse_cidrs(",".join(raw), label)


def _outside(nets, bound) -> List[str]:
    """Entries with no address in common with the environment list: they can
    never let anyone in, which is almost certainly a mistake worth showing."""
    if not bound:
        return []
    return [str(n) for n in nets if not any(n.version == b.version and n.overlaps(b) for b in bound)]


async def _put(request: web.Request) -> web.Response:
    svc = _svc(request)
    policy: NetPolicy = svc.netpolicy
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    reject_unknown_fields(body, {"uiAllow", "edgeAllow", "password"})

    try:
        ui = _parse_list(body, "uiAllow", "Web UI and API")
        edge = _parse_list(body, "edgeAllow", "Edge agents")
    except ValueError as err:
        return error_response(400, str(err))

    # A stolen session alone must not be able to change who may connect.
    cu = user_from(request)
    try:
        user = svc.store.get_user(cu.id) if cu is not None else None
    except Exception:  # noqa: BLE001
        user = None
    if user is None:
        return error_response(403, "only a signed-in admin can change network access")
    if user.password_hash:
        from kowan.api.authapi import _limiter

        password = body.get("password") or ""
        if not isinstance(password, str) or not password:
            return error_response(403, "confirm with your password")
        if not await _limiter(svc).hashing(verify_password, user.password_hash, password):
            return error_response(403, "wrong password")

    # Refuse a list that would lock out the very connection saving it.
    candidate = NetPolicy(
        trusted_proxies=policy.trusted_proxies, ui_allow=policy.ui_allow, agent_allow=policy.agent_allow,
        proxy_header=policy.proxy_header, ui_allow_stored=ui, agent_allow_stored=edge,
    )
    addr = policy.client_address(request)
    if ui and not candidate.address_allowed(addr):
        shown = str(addr) if addr is not None else "unknown"
        return error_response(409, f"this list would lock you out: your address ({shown}) is not in it")

    svc.store.set_setting(UI_SETTING, ",".join(_strs(ui)))
    svc.store.set_setting(EDGE_SETTING, ",".join(_strs(edge)))
    policy.ui_allow_stored, policy.agent_allow_stored = ui, edge

    out = _view(request, policy)
    out["warnings"] = [
        f"{n} is outside the environment's Web UI list, so it lets nobody in" for n in _outside(ui, policy.ui_allow)
    ] + [
        f"{n} is outside the environment's edge list, so it lets nobody in" for n in _outside(edge, policy.agent_allow)
    ]
    return json_response(out)
