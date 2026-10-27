"""Application skeleton + dependency-injection container for the control plane.

This module is the backbone the whole api layer depends on. It defines:

  1. ``Services`` — the DI container carrying the shared singletons every api
     module needs (config, store, jwt, bus, cipher, docker provider, tunnel
     hub, and the audit recorder). Extra singletons may be attached freely.

  2. ``create_app(svc)`` — builds the aiohttp Application, installs the shared
     middleware chain (request-id -> logging -> recover/error-to-JSON -> CORS
     -> auth -> audit), and calls each api module's ``register(app, svc)``.

  3. The ``register(app, svc)`` convention every api module MUST follow, plus
     helpers (``api_route``, ``require_role``, ``add_public_route``) that mount
     routes under the correct prefix and declare a route's required role.

  4. SPA serving (static from ../web/dist with index.html fallback) and the
     public webhook mount point.
"""

from __future__ import annotations

import logging
import mimetypes
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from aiohttp import web

from .accesslog import redact
from .responses import error_response

# The auth module is the AUTHORITATIVE owner of the request-identity contract:
# it defines REQUEST_USER_KEY ("user"), the CtxUser principal, the
# auth_middleware(jwt, keys) factory, require_role(), and user_from(). We
# re-export these so the whole web/api layer has one consistent source.
from ..auth.middleware import (  # noqa: F401 - re-exported for the api layer
    REQUEST_USER_KEY,
    auth_middleware as _auth_middleware_factory,
    require_role,
    user_from,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids import cycles
    from ..config import ServerConfig
    from ..store import Store
    from ..auth.jwt import JWT
    from ..auth.apikey import APIKeyVerifier
    from ..events import Bus
    from ..secrets import Cipher
    from ..docker import Provider
    from ..tunnel.hub import Hub

log = logging.getLogger("kowan.web")

# ---------------------------------------------------------------------------
# Shared request/app keys — the single agreed identifiers the whole api layer
# reads. Keeping them here (not scattered) is the contract downstream modules
# rely on. REQUEST_USER_KEY is re-exported from kowan.auth.middleware above.
# ---------------------------------------------------------------------------

# Request key carrying the per-request id. Set by the
# request-id middleware; surfaced in logs and the X-Request-Id response header.
REQUEST_ID_KEY: "web.RequestKey[str]" = web.RequestKey("kowan_request_id", str)

# aiohttp Application key holding the Services container, so middleware and
# handlers can reach the DI singletons via ``request.app[SERVICES_KEY]``.
SERVICES_KEY = web.AppKey("kowan_services", object)

# Application key holding the set of PUBLIC absolute paths that must bypass the
# auth (and audit) middleware even though they live under /api/v1 — e.g.
# POST /api/v1/auth/login and the SSO provider-list / OAuth callback routes.
# ``add_public_route`` registers them here; the auth scoping wrapper consults it.
PUBLIC_PATHS_KEY = web.AppKey("kowan_public_paths", set)

# The prefix every protected api module mounts its routes under.
API_PREFIX = "/api/v1"


# ---------------------------------------------------------------------------
# DI container
# ---------------------------------------------------------------------------


@dataclass
class Services:
    """Dependency-injection container carrying the shared singletons.

    Constructed once at startup (kowan/server.py) and threaded to every api
    module via
    ``register(app, svc)``. Fields are typed with TYPE_CHECKING forward refs so
    this module has no hard import dependency on the concrete singletons (which
    would create import cycles), and every field except ``config`` defaults to
    None so partially-wired instances are valid in tests.
    """

    config: "ServerConfig"
    store: Optional["Store"] = None
    jwt: Optional["JWT"] = None
    # API-key verifier passed to the auth middleware (auth.APIKeyVerifier). May
    # be None ("no API key support").
    keys: Optional["APIKeyVerifier"] = None
    bus: Optional["Bus"] = None
    cipher: Optional["Cipher"] = None
    # The per-host Docker Ops provider.
    docker: Optional["Provider"] = None
    # The edge tunnel hub.
    hub: Optional["Hub"] = None
    # AuditRecorder: an object exposing ``record(entry)`` (sync or async),
    # stamping the Time field itself. When None, audit recording is disabled.
    audit: Optional[Any] = None

    # Escape hatch for additional singletons a later phase needs to attach
    # without editing this dataclass (e.g. deployment service, workflow engine).
    extra: dict = field(default_factory=dict)

    def attach(self, name: str, value: Any) -> "Services":
        """Attach an extra singleton, reachable as ``svc.name`` or via extra."""
        self.extra[name] = value
        return self

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails; look in extra.
        extra = self.__dict__.get("extra")
        if extra is not None and name in extra:
            return extra[name]
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# Route-mounting helpers — the register(app, svc) convention.
# ---------------------------------------------------------------------------

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# NOTE: ``require_role`` is re-exported from kowan.auth.middleware (imported at
# the top of this module) — it is the authoritative role gate. Handlers declare
# their required role by wrapping themselves with it, e.g.::
#
#     @require_role("admin")
#     async def create_secret(request): ...
#
# Admins are always allowed; other roles must be listed; no principal -> 401,
# disallowed role -> 403. The auth middleware must have run first.


def api_route(
    app: web.Application,
    method: str,
    path: str,
    handler: Handler,
) -> None:
    """Mount a single protected route under the ``/api/v1`` prefix.

    ``path`` is given WITHOUT the prefix (e.g. "/secrets/{id}"); this helper
    prepends ``API_PREFIX``. Wrap ``handler`` with ``require_role(...)`` first
    to gate it by role. This is the standard way api modules add routes inside
    their ``register(app, svc)``.
    """
    full = API_PREFIX + path
    app.router.add_route(method.upper(), full, handler)


def add_public_route(
    app: web.Application,
    method: str,
    path: str,
    handler: Handler,
) -> None:
    """Mount a route at an absolute path OUTSIDE the ``/api/v1`` group.

    For public endpoints that authenticate via a token in the request itself
    (login, /hooks/*, /edge/connect, OAuth callbacks). ``path`` is absolute.

    The path is recorded in ``app[PUBLIC_PATHS_KEY]`` so the auth/audit scoping
    wrapper lets it through even when it lives under /api/v1 (e.g.
    ``/api/v1/auth/login``). Paths outside /api/v1 are never gated regardless.
    """
    _public_paths(app).add(path)
    app.router.add_route(method.upper(), path, handler)


def _public_paths(app: web.Application) -> set:
    """Return (creating if needed) the app's set of public absolute paths."""
    try:
        return app[PUBLIC_PATHS_KEY]
    except KeyError:
        s: set = set()
        app[PUBLIC_PATHS_KEY] = s
        return s


def bind_svc(
    svc: "Services",
) -> Callable[[Callable[["Services", web.Request], Awaitable[web.StreamResponse]]], Handler]:
    """Return a wrapper that binds ``svc`` into ``(svc, request)``-style handlers.

    Most api modules write their handlers as ``async def handler(svc, request)``
    and need a small closure to adapt them to aiohttp's ``handler(request)``
    signature, capturing the module-local ``svc``. This is that closure, shared
    so each ``register(app, svc)`` can do ``h = bind_svc(svc)`` instead of
    redefining the identical wrapper.
    """

    def h(fn: Callable[["Services", web.Request], Awaitable[web.StreamResponse]]) -> Handler:
        async def handler(request: web.Request) -> web.StreamResponse:
            return await fn(svc, request)

        return handler

    return h


def now_rfc3339() -> str:
    """Current time as an RFC3339 UTC timestamp (``...Z`` suffix, second precision).

    The single canonical timestamp helper every api module uses when stamping
    ``createdAt`` / ``updatedAt``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Middleware chain.
# ---------------------------------------------------------------------------


@web.middleware
async def request_id_middleware(request: web.Request, handler):
    """Assign a per-request id (reusing an inbound X-Request-Id)."""
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request[REQUEST_ID_KEY] = rid
    response = await handler(request)
    response.headers.setdefault("X-Request-Id", rid)
    return response


# Responses smaller than this are sent as-is: gzip framing overhead and CPU are
# not worth it for a tiny JSON envelope.
_COMPRESS_MIN_BYTES = 1024

# Text-like content types worth gzipping (JSON api bodies + SPA assets).
_COMPRESSIBLE_TYPES = (
    "application/json",
    "application/javascript",
    "text/",
    "image/svg+xml",
    "application/manifest+json",
)


def _accepts_gzip(request: web.Request) -> bool:
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


@web.middleware
async def compression_middleware(request: web.Request, handler):
    """Gzip buffered text/JSON responses when the client accepts it.

    List endpoints (containers, images, metrics, audit) return JSON that
    compresses 5-10x; on a remote/edge link this dominates page load. Only
    fully-buffered ``web.Response`` bodies above a small threshold are touched —
    streams, WebSockets and file responses are left alone (the SPA handler
    enables compression for its static files itself).
    """
    response = await handler(request)
    if (
        type(response) is web.Response
        and not response.prepared
        and isinstance(response.body, (bytes, bytearray))
        and len(response.body) >= _COMPRESS_MIN_BYTES
        and "Content-Encoding" not in response.headers
        and response.content_type.startswith(_COMPRESSIBLE_TYPES)
        and _accepts_gzip(request)
    ):
        from aiohttp.hdrs import VARY
        from aiohttp.web import ContentCoding

        response.enable_compression(ContentCoding.gzip)
        vary = response.headers.get(VARY, "")
        if "accept-encoding" not in vary.lower():
            response.headers[VARY] = (vary + ", " if vary else "") + "Accept-Encoding"
    return response


@web.middleware
async def logging_middleware(request: web.Request, handler):
    """Log each request after it completes."""
    start = time.monotonic()
    status = 500
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        dur = time.monotonic() - start
        log.info(
            "http request method=%s path=%s status=%s duration=%.3fs remote=%s reqID=%s",
            request.method,
            redact(request.rel_url.path),
            status,
            dur,
            request.remote,
            request.get(REQUEST_ID_KEY, ""),
        )


@web.middleware
async def recover_middleware(request: web.Request, handler):
    """Recover from unhandled exceptions and convert them to JSON errors.

    Uses the shared JSON error envelope: HTTPExceptions already carrying a body pass through; any other
    exception becomes a 500 ``{"error": "internal server error"}``.
    """
    from kowan.errors import AppError

    try:
        return await handler(request)
    except web.HTTPException as exc:
        # HTTPExceptions raised by handlers/helpers (e.g. responses.JSONError) are
        # intentional responses — let aiohttp render them.
        if exc.content_type == "application/json" or exc.text:
            raise
        return error_response(exc.status, exc.reason or "error")
    except AppError as err:
        # Domain errors carry their own HTTP status + message (Validation=400,
        # NotFound=404, Conflict=409, Unauthorized=401, Forbidden=403, ...).
        return error_response(err.status, err.message)
    except Exception:  # noqa: BLE001 - last-resort guard, never leak a stack
        log.exception("panic recovered in handler for %s %s", request.method, request.rel_url.path)
        return error_response(500, "internal server error")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Reflect the request Origin for cross-origin API/token use, short-circuit
    preflight OPTIONS.

    Deliberately WITHOUT
    ``Access-Control-Allow-Credentials``: auth here is a bearer token in the
    Authorization header (never a cookie), so credentials mode is unnecessary,
    and reflecting an arbitrary Origin *together* with allow-credentials would be
    an over-permissive combination. Dropping it keeps token auth working while
    denying a cross-origin page any cookie-backed access.
    """
    origin = request.headers.get("Origin") or "*"
    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)
    h = response.headers
    h["Access-Control-Allow-Origin"] = origin
    h["Vary"] = "Origin"
    h["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    h["Access-Control-Allow-Headers"] = "Accept, Authorization, Content-Type, X-Requested-With"
    h["Access-Control-Expose-Headers"] = "Authorization"
    h["Access-Control-Max-Age"] = "300"
    return response


def _is_protected(request: web.Request) -> bool:
    """Report whether the request path is inside the authenticated /api/v1 group.

    A path is protected when it starts with ``/api/v1`` AND is not registered as
    a public exemption via ``add_public_route`` (e.g. /api/v1/auth/login). Every
    non-/api/v1 path (health, /hooks/*, /edge/connect, the SPA) is public:
    only the /api/v1 group carries the auth middleware.
    """
    path = request.rel_url.path
    if not path.startswith(API_PREFIX):
        return False
    # Read-only here: a running app must not be mutated (aiohttp deprecation).
    public = request.app.get(PUBLIC_PATHS_KEY) or frozenset()
    if path in public:
        return False
    # Parametrized public routes (e.g. /api/v1/auth/{id}/start) are registered
    # by their pattern, which never equals the concrete request path. aiohttp has
    # already resolved routing before middleware runs, so consult the matched
    # route's canonical pattern too.
    route = request.match_info.route if request.match_info else None
    resource = route.resource if route is not None else None
    canonical = getattr(resource, "canonical", None) if resource is not None else None
    if canonical is not None and canonical in public:
        return False
    return True


def _scoped(inner):
    """Wrap a middleware so it only runs for protected /api/v1 requests.

    Public paths bypass ``inner`` entirely. This gives route-group scoping
    (auth + audit apply only under the /api/v1 group) on top of aiohttp's flat
    global-middleware model.
    """

    @web.middleware
    async def scoped(request: web.Request, handler):
        if _is_protected(request):
            return await inner(request, handler)
        return await handler(request)

    return scoped


def _build_auth_middleware(svc: "Services"):
    """Build the (path-scoped) auth middleware from the DI container.

    Wires ``kowan.auth.middleware.auth_middleware(jwt, keys)`` from
    ``svc.jwt`` / ``svc.keys`` and scopes it to protected /api/v1 paths. Returns
    None when ``svc.jwt`` is unset (tests / early boot leave the /api/v1 group
    unauthenticated — require_role then 401s on any protected handler).
    """
    if svc.jwt is None:
        return None
    sessions = None
    if svc.store is not None:
        from ..auth.sessions import make_session_checker

        sessions = make_session_checker(svc.store)
    return _scoped(_auth_middleware_factory(svc.jwt, svc.keys, sessions))


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------


def create_app(
    svc: "Services",
    *,
    registrars: Optional[list[Callable[[web.Application, "Services"], None]]] = None,
) -> web.Application:
    """Build the aiohttp Application with the shared middleware chain and routes.

    Middleware order (outermost first):
      request-id -> logging -> recover/error-to-JSON -> CORS -> auth -> audit.

    ``registrars`` is the ordered list of api-module ``register`` callables
    (each ``register(app, svc) -> None``); they mount protected routes under
    ``/api/v1`` (via ``api_route``) and public routes (via ``add_public_route``).
    """
    from .audit import audit_middleware  # local import: avoids app<->audit cycle

    from .netpolicy import NETPOLICY_KEY, NetPolicy, netpolicy_middleware

    # Raises on a malformed CIDR: better to refuse to start than to run open.
    netpolicy = NetPolicy.from_config(svc.config)
    if svc.store is not None:
        netpolicy.load_stored(svc.store)
    svc.attach("netpolicy", netpolicy)
    from .headers import security_headers_middleware

    https_external = (getattr(svc.config, "external_url", "") or "").lower().startswith("https://")
    middlewares = [
        request_id_middleware,
        security_headers_middleware(https_external),
        netpolicy_middleware,
        compression_middleware,
        logging_middleware,
        recover_middleware,
        cors_middleware,
    ]
    auth_mw = _build_auth_middleware(svc)
    if auth_mw is not None:
        middlewares.append(auth_mw)
    # Audit runs AFTER auth so the principal is available, and is scoped to the
    # protected /api/v1 group (never records public paths). A no-op when
    # svc.audit is None.
    middlewares.append(_scoped(audit_middleware(svc)))

    app = web.Application(middlewares=middlewares, client_max_size=1 << 20)
    app[SERVICES_KEY] = svc
    app[NETPOLICY_KEY] = netpolicy
    from .wsregistry import install as _install_ws_registry

    _install_ws_registry(app)

    # Health check (public): GET /health -> "ok".
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain", charset="utf-8")

    app.router.add_get("/health", health)

    # Mount every api module's routes.
    for register in registrars or []:
        register(app, svc)

    return app


# ---------------------------------------------------------------------------
# SPA + static serving.
# ---------------------------------------------------------------------------


# Files at the root of the build that must not be cached as-is (PWA).
_REVALIDATE = frozenset({"sw.js", "manifest.webmanifest"})


def _dist_dir() -> Path:
    """Absolute path to the built SPA (../web/dist relative to this file)."""
    # kowan/web/app.py -> repo/web/dist
    return (Path(__file__).resolve().parents[2] / "web" / "dist").resolve()


def add_spa(app: web.Application, dist: Optional[Path] = None) -> None:
    """Serve the built SPA with client-side-routing fallback to index.html.

    API and hook paths never fall back to the
    SPA (they 404); an existing static file is served directly; any other path
    returns index.html so the React router can handle it. Must be added AFTER
    all api/hook routes so it only catches unmatched paths.
    """
    # Resolve once: candidates are resolved below, and relative_to() against an
    # unresolved (relative / symlinked) root would reject every real file.
    root = (dist or _dist_dir()).resolve()
    index = root / "index.html"
    assets_dir = root / "assets"

    def _file(path: Path, request: web.Request) -> web.FileResponse:
        resp = web.FileResponse(path)
        if path.parent == assets_dir:
            # Vite emits content-hashed names under assets/: they never change,
            # so let the browser keep them forever and skip revalidation.
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == index or path.name in _REVALIDATE:
            # index.html points at the current hashed bundles: always revalidate
            # (cheap 304 via ETag/Last-Modified) so a new build is picked up. The
            # service worker and the manifest change with a build too.
            resp.headers["Cache-Control"] = "no-cache"
        if path.suffix == ".webmanifest":
            resp.content_type = "application/manifest+json"
        ctype = mimetypes.guess_type(path.name)[0] or ""
        if ctype.startswith(_COMPRESSIBLE_TYPES) and _accepts_gzip(request):
            # FileResponse prefers a pre-compressed sibling (x.js.gz / x.js.br)
            # when one exists and only falls back to on-the-fly gzip otherwise.
            from aiohttp.web import ContentCoding

            resp.enable_compression(ContentCoding.gzip)
        return resp

    async def spa_handler(request: web.Request) -> web.StreamResponse:
        path = request.rel_url.path
        if path.startswith("/api/") or path.startswith("/hooks"):
            raise web.HTTPNotFound()

        rel = path.lstrip("/") or "index.html"
        candidate = (root / rel).resolve()
        # Guard against path traversal escaping the dist root.
        try:
            candidate.relative_to(root)
        except ValueError:
            candidate = index
        if candidate.is_file():
            return _file(candidate, request)
        if index.is_file():
            return _file(index, request)
        raise web.HTTPNotFound()

    # Catch-all for any unmatched GET/HEAD path.
    app.router.add_route("GET", "/{tail:.*}", spa_handler)
    app.router.add_route("HEAD", "/{tail:.*}", spa_handler)


def add_webhooks(
    app: web.Application,
    handler: Handler,
    *,
    prefix: str = "/hooks",
) -> None:
    """Mount the public webhook receiver under ``/hooks`` (default).

    The webhook worker authenticates via the per-hook token in the path itself,
    so no auth middleware gates it. More
    specific hook prefixes (/hooks/deploy, /hooks/workflow) should be added via
    ``add_public_route`` BEFORE this catch-all so they take precedence.
    """
    app.router.add_route("*", prefix + "/{tail:.*}", handler)
    app.router.add_route("*", prefix, handler)
