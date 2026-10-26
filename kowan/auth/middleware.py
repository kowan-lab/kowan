"""aiohttp authentication + authorization middleware.

Each request is authenticated via an
API key (tried first) or a JWT bearer token; on success the resulting principal
is stored on the request so downstream handlers can read it, and a 401 is
returned on failure. :func:`require_role` is the per-route authorization gate.

Request identity contract
-------------------------
After the middleware runs, the authenticated principal is available as::

    request[REQUEST_USER_KEY]     # -> a CtxUser

Downstream API handlers read ``user_from(request)`` (a :class:`CtxUser` with
``.id`` / ``.username`` / ``.role``). Use :func:`user_from` for a safe lookup
that returns ``None`` when unauthenticated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from aiohttp import web

from .jwt import JWT, JWTError
from .sessions import SessionChecker
from .apikey import APIKeyVerifier, API_KEY_SECRET_PREFIX, hash_api_key_secret
from .authorize import bind_request, unbind

# REQUEST_USER_KEY is the aiohttp request key under which the authenticated
# principal (a CtxUser) is stored; read it with ``user_from(request)``.
REQUEST_USER_KEY: "web.RequestKey[Any]" = web.RequestKey("kowan_user", object)

# Role constants. Kept here so the middleware has no import dependency on the
# models package.
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"


@dataclass
class CtxUser:
    """The authenticated principal carried on a request after the middleware
    has validated the credential.
    """

    id: str
    username: str
    role: str
    # SHA-256 hex of the presented API key secret (API-key principals only);
    # kowan.auth.authorize uses it to load the key's team / narrowed scopes.
    key_hash: str = ""


def user_from(request: web.Request) -> Optional[CtxUser]:
    """Return the authenticated :class:`CtxUser`, or ``None`` if absent."""
    return request.get(REQUEST_USER_KEY)


def _json_error(status: int, msg: str) -> web.Response:
    """Write a JSON ``{"error": msg}`` body (the shared error envelope)."""
    return web.json_response({"error": msg}, status=status)


def _is_websocket_upgrade(request: web.Request) -> bool:
    """Report whether ``request`` is a WebSocket upgrade handshake.

    Browsers cannot set the Authorization header on a WebSocket handshake, so
    such requests may carry the token in ``?token=`` instead.
    """
    connection = request.headers.get("Connection", "").lower()
    upgrade = request.headers.get("Upgrade", "")
    return "upgrade" in connection and upgrade.lower() == "websocket"


def _api_key_secret(request: web.Request, authz: str) -> str:
    """Extract an API key secret, or ``""`` when the request presents none.

    An ``X-API-Key`` header takes precedence; otherwise a ``Bearer`` value
    (``authz`` — which already folds in the WS ``?token=`` fallback) is treated
    as an API key only when it begins with the API key scheme prefix.
    """
    xkey = request.headers.get("X-API-Key", "").strip()
    if xkey:
        return xkey
    parts = authz.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        tok = parts[1].strip()
        if tok.startswith(API_KEY_SECRET_PREFIX):
            return tok
    return ""


def auth_middleware(jwt: JWT, keys: Optional[APIKeyVerifier], sessions: Optional[SessionChecker] = None):
    """Build an aiohttp ``@middleware`` that authenticates each request.

    API keys are tried first: a request is
    key-authenticated when it carries an ``X-API-Key`` header, an
    ``Authorization: Bearer`` value beginning with ``"pk_"``, or (for WebSocket
    upgrades only) a ``?token=`` beginning with ``"pk_"``. The secret is verified
    via ``keys`` (when non-``None``); on success the principal is a synthetic
    ``CtxUser(id="apikey", username=<key name>, role=<key role>)``. Otherwise the
    request falls back to JWT bearer validation.

    For WebSocket upgrade requests only, the credential may be supplied via the
    ``?token=`` query parameter (for both JWT and API keys); ordinary REST
    requests never read credentials from the URL.

    ``sessions`` (kowan.auth.sessions) re-checks a JWT against the database:
    deleted users, revoked tokens and stale session versions get a 401, and the
    role is taken from the database rather than the token.

    On success the :class:`CtxUser` is stored at ``request[REQUEST_USER_KEY]``.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        authz = request.headers.get("Authorization", "")
        # WebSocket upgrades cannot set headers; accept the credential from
        # ?token= and reshape it as the equivalent Bearer header form.
        if authz == "" and _is_websocket_upgrade(request):
            tok = request.query.get("token", "").strip()
            if tok:
                authz = "Bearer " + tok

        # API key path: an explicit X-API-Key header, or a Bearer/?token= secret
        # beginning with the API key scheme prefix.
        secret = _api_key_secret(request, authz)
        if secret:
            if keys is not None:
                role, username, ok = await keys.verify(secret)
                if ok:
                    user = CtxUser(
                        id="apikey", username=username, role=role,
                        key_hash=hash_api_key_secret(secret),
                    )
                    request[REQUEST_USER_KEY] = user
                    return await _with_authz(request, user, handler)
            return _json_error(401, "invalid api key")

        if authz == "":
            return _json_error(401, "missing authorization header")
        parts = authz.split(" ", 1)
        if (
            len(parts) != 2
            or parts[0].lower() != "bearer"
            or parts[1].strip() == ""
        ):
            return _json_error(401, "invalid authorization header")

        try:
            claims = jwt.verify(parts[1].strip())
        except JWTError:
            return _json_error(401, "invalid token")

        role, username = claims.role, claims.username
        if sessions is not None:
            live = sessions(claims)
            if live is None:
                return _json_error(401, "session expired")
            role, username = live
        user = CtxUser(id=claims.sub, username=username, role=role)
        request[REQUEST_USER_KEY] = user
        return await _with_authz(request, user, handler)

    return middleware


async def _with_authz(request: web.Request, user: CtxUser, handler):
    """Run ``handler`` with the request's authorization context bound (see
    kowan.auth.authorize): Ops resolution and the API helpers read it."""
    token = bind_request(request, user)
    try:
        return await handler(request)
    finally:
        unbind(token)


def require_role(*roles: str):
    """Decorator gating a handler to the given roles.

    Admins are always allowed; a 403 is returned
    when the role is not permitted, and a 401 when no authenticated user is
    present on the request. Apply it to an aiohttp handler::

        @require_role(ROLE_OPERATOR)
        async def handler(request): ...
    """
    allowed = set(roles)

    def decorator(handler):
        async def wrapped(request: web.Request):
            user = user_from(request)
            if user is None:
                return _json_error(401, "authentication required")
            if user.role == ROLE_ADMIN:
                return await handler(request)
            if user.role not in allowed:
                return _json_error(403, "insufficient permissions")
            return await handler(request)

        return wrapped

    return decorator
