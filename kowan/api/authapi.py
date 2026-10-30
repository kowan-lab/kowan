"""Authentication + user-management HTTP API (local accounts only).

Provides:

  * ``POST /auth/login``   (public) — local username/password login -> JWT.
  * ``POST /auth/logout``  — client-side session teardown (JWTs are stateless;
                             this is a courtesy endpoint the SPA calls).
  * ``GET  /me``           — the current authenticated user.
  * ``PUT  /me/password``  — any user changes their own password (the current
                             one is required).
  * ``/users`` CRUD        — admin only.

Wire it up from the server bootstrap with ``register(app, svc)``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aiohttp import web

from kowan.auth import hash_password, verify_password, require_role, user_from
from kowan.auth.jwt import JWTError
from kowan.auth.ratelimit import LoginLimiter
from kowan.web.netpolicy import client_ip
from kowan.auth.setup import discard_setup_token, setup_token_matches
from kowan.models import User
from kowan.models.user import (
    AuthSourceLocal,
    RoleAdmin,
    RoleOperator,
    RoleViewer,
)
from kowan.web import (
    SERVICES_KEY,
    add_public_route,
    api_route,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    reject_unknown_fields,
)

log = logging.getLogger("kowan.api.authapi")


_DUMMY_HASH: list = []


def _dummy_hash() -> str:
    """A lazily-built scrypt hash of a random secret, verified for unknown
    usernames so a failed login costs the same as a wrong password."""
    if not _DUMMY_HASH:
        import secrets as _secrets

        _DUMMY_HASH.append(hash_password(_secrets.token_hex(16)))
    return _DUMMY_HASH[0]


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def _valid_role(role: str) -> bool:
    return role in (RoleAdmin, RoleOperator, RoleViewer)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(app: web.Application, svc) -> None:
    """Mount authentication and user-management routes.

    ``/auth/login`` is public (bypasses the JWT middleware); ``/me``
    requires any authenticated principal; ``/users`` CRUD requires
    admin.
    """
    # Public.
    add_public_route(app, "POST", "/api/v1/auth/login", _login)
    # First-run setup: status tells the SPA whether an admin
    # exists yet; init creates the very first admin when none do.
    add_public_route(app, "GET", "/api/v1/auth/status", _status)
    add_public_route(app, "POST", "/api/v1/auth/init", _init)

    # Protected.
    api_route(app, "GET", "/me", _me)
    api_route(app, "PUT", "/me/password", _change_own_password)
    api_route(app, "POST", "/auth/logout", _logout)

    api_route(app, "GET", "/users", require_role(RoleAdmin)(_list_users))
    api_route(app, "POST", "/users", require_role(RoleAdmin)(_create_user))
    api_route(app, "GET", "/users/{id}", require_role(RoleAdmin)(_get_user))
    api_route(app, "PUT", "/users/{id}", require_role(RoleAdmin)(_update_user))
    api_route(app, "DELETE", "/users/{id}", require_role(RoleAdmin)(_delete_user))


# ---------------------------------------------------------------------------
# Login (local username/password)
# ---------------------------------------------------------------------------


async def _login(request: web.Request) -> web.Response:
    """Authenticate by username/password, returning a JWT + user record."""
    svc = _svc(request)
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if username == "" or password == "":
        return error_response(400, "username and password are required")
    if len(username) > 256 or len(password) > 1024:
        return error_response(400, "username or password too long")

    limiter = _limiter(svc)
    address = _client_ip(request)
    wait = limiter.retry_after(address, username)
    if wait is not None or limiter.busy():
        _record_login(svc, request, "login_throttled", "", username, 429)
        return _too_many(wait or 5)

    # SSO-only mode (kowan/api/sso.py): only the break-glass local admin may
    # still sign in with a password.
    from kowan.api import sso as _sso

    sso_only = _sso.sso_only(svc)
    if sso_only and username != _sso.break_glass_user(svc):
        _record_login(svc, request, "login_failed", "", username, 403)
        return error_response(403, "password sign-in is disabled; use single sign-on")

    try:
        u = svc.store.get_user_by_username(username)
    except Exception:  # noqa: BLE001 - store NotFoundError => no local account
        u = None
    # scrypt costs ~50ms of CPU: run it off the event loop so a burst of logins
    # (or a brute-force attempt) cannot stall every other request. Unknown users
    # are checked against a dummy hash so the response time does not reveal
    # whether the username exists.
    if u is not None and u.password_hash != "":
        stored = u.password_hash
    else:
        stored = await asyncio.to_thread(_dummy_hash)
    ok = await limiter.hashing(verify_password, stored, password)
    if ok and u is not None and u.password_hash != "":
        if sso_only and not _sso.password_login_allowed(svc, u):
            _record_login(svc, request, "login_failed", u.id, username, 403)
            return error_response(403, "password sign-in is disabled; use single sign-on")
        limiter.succeeded(address, username)
        return _issue_login(svc, request, u)

    limiter.failed(address, username)
    _record_login(svc, request, "login_failed", "", username, 401)
    return error_response(401, "invalid credentials")


MIN_PASSWORD_LENGTH = 8


def _limiter(svc) -> LoginLimiter:
    limiter = getattr(svc, "login_limiter", None)
    if limiter is None:
        limiter = LoginLimiter()
        svc.attach("login_limiter", limiter)
    return limiter


def _too_many(retry_after: int) -> web.Response:
    resp = error_response(429, "too many sign-in attempts; try again later")
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def _issue_login(svc, request: web.Request, u: User) -> web.Response:
    """Sign a JWT for ``u``, record the success, and write the login response."""
    try:
        token = svc.jwt.issue(u.id, u.username, u.role, svc.store.session_version(u.id))
    except Exception as err:  # noqa: BLE001
        log.error("issue jwt: %s username=%s", err, u.username)
        return error_response(500, "failed to issue token")
    _record_login(svc, request, "login", u.id, u.username, 200)
    return json_response({"token": token, "user": u})


# ---------------------------------------------------------------------------
# First-run setup (public): status + initial-admin creation
# ---------------------------------------------------------------------------


async def _status(request: web.Request) -> web.Response:
    """Public: report whether the control plane has been initialized.

    ``initialized`` is true once any user exists. The SPA calls this before the
    login screen to decide whether to show the first-run setup form instead.
    """
    svc = _svc(request)
    try:
        initialized = svc.store.count_users() > 0
    except Exception as err:  # noqa: BLE001
        log.error("auth status: count users failed: %s", err)
        return error_response(500, "failed to read status")
    return json_response({"initialized": initialized})


async def _init(request: web.Request) -> web.Response:
    """Public first-run setup: create the initial admin when no users exist.

    Guarded so it is a one-shot: once any account exists it returns 409, so it
    can never add users to an initialized server. On success it returns a JWT +
    user (like login) so the browser is signed straight in.
    """
    svc = _svc(request)
    try:
        if svc.store.count_users() > 0:
            return error_response(409, "already initialized")
    except Exception as err:  # noqa: BLE001
        log.error("auth init: count users failed: %s", err)
        return error_response(500, "failed to read status")

    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    # Prove access to the server (its logs or data dir): otherwise whoever
    # reaches a fresh instance first becomes admin, i.e. root on the host.
    limiter = _limiter(svc)
    address = _client_ip(request)
    wait = limiter.retry_after(address, None)
    if wait is not None:
        return _too_many(wait)
    if not setup_token_matches(getattr(svc, "setup_token", None), body.get("setupToken")):
        limiter.failed(address, None)
        return error_response(403, "invalid setup token")

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if username == "" or password == "":
        return error_response(400, "username and password are required")
    if len(password) < MIN_PASSWORD_LENGTH:
        return error_response(400, f"password must be at least {MIN_PASSWORD_LENGTH} characters")

    admin = User(
        id=str(uuid.uuid4()),
        username=username,
        role=RoleAdmin,
        password_hash=await asyncio.to_thread(hash_password, password),
        authSource=AuthSourceLocal,
        createdAt=now_rfc3339(),
    )
    try:
        # Re-check right before the insert (no await in between, so this is
        # atomic on the event loop): the guard above ran before the body read
        # and the password hash, so two concurrent setup requests could both
        # have passed it and each created an admin.
        if svc.store.count_users() > 0:
            return error_response(409, "already initialized")
        svc.store.create_user(admin)
    except Exception as err:  # noqa: BLE001
        log.error("auth init: create admin failed: %s", err)
        return error_response(500, "failed to create admin")
    log.info("first-run setup: created initial admin %r", username)
    svc.attach("setup_token", None)
    discard_setup_token(svc.config.data_dir)
    return _issue_login(svc, request, admin)


# ---------------------------------------------------------------------------
# Current user / logout
# ---------------------------------------------------------------------------


async def _me(request: web.Request) -> web.Response:
    """Return the currently authenticated user."""
    svc = _svc(request)
    cu = user_from(request)
    if cu is None:
        return error_response(401, "not authenticated")
    try:
        u = svc.store.get_user(cu.id)
    except Exception:  # noqa: BLE001 - store NotFoundError
        return error_response(404, "user not found")
    return json_response(u)


async def _change_own_password(request: web.Request) -> web.Response:
    """Change the caller's own password from ``{currentPassword, newPassword}``.

    Open to every signed-in user (admins manage other accounts through
    ``PUT /users/{id}``). The current password is checked under the sign-in
    rate limit, so a stolen session cannot guess it. Every other session ends;
    this one carries on with the token in ``X-Session-Token``.
    """
    svc = _svc(request)
    cu = user_from(request)
    if cu is None:
        return error_response(401, "not authenticated")
    if getattr(cu, "key_hash", ""):
        return error_response(400, "an API key has no password")
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    reject_unknown_fields(body, {"currentPassword", "newPassword"})
    current = body.get("currentPassword") or ""
    new = body.get("newPassword") or ""
    if not isinstance(current, str) or not isinstance(new, str) or current == "" or new == "":
        return error_response(400, "currentPassword and newPassword are required")
    if len(new) < MIN_PASSWORD_LENGTH:
        return error_response(400, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(current) > 1024 or len(new) > 1024:
        return error_response(400, "password too long")
    try:
        u = svc.store.get_user(cu.id)
    except Exception:  # noqa: BLE001 - store NotFoundError
        return error_response(404, "user not found")
    if u.password_hash == "":
        return error_response(400, "this account signs in with single sign-on and has no password")

    limiter = _limiter(svc)
    address = _client_ip(request)
    wait = limiter.retry_after(address, u.username)
    if wait is not None or limiter.busy():
        return _too_many(wait or 5)
    if not await limiter.hashing(verify_password, u.password_hash, current):
        limiter.failed(address, u.username)
        return error_response(400, "current password is incorrect")
    limiter.succeeded(address, u.username)

    try:
        u.password_hash = await asyncio.to_thread(hash_password, new)
        svc.store.update_user(u)
        svc.store.bump_session_version(u.id)
    except Exception as err:  # noqa: BLE001
        log.error("change own password: %s id=%s", err, u.id)
        return error_response(500, "failed to update password")
    resp = json_response(u)
    resp.headers["X-Session-Token"] = svc.jwt.issue(u.id, u.username, u.role, svc.store.session_version(u.id))
    return resp


async def _logout(request: web.Request) -> web.Response:
    """End this session: the token's id is revoked until it would expire."""
    svc = _svc(request)
    parts = request.headers.get("Authorization", "").split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        try:
            claims = svc.jwt.verify(parts[1].strip())
        except JWTError:
            claims = None
        if claims is not None and claims.jti:
            svc.store.revoke_token(claims.jti, claims.exp)
    return json_response({"status": "logged out"})


# ---------------------------------------------------------------------------
# User CRUD (admin only)
# ---------------------------------------------------------------------------


async def _list_users(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        users = svc.store.list_users()
    except Exception as err:  # noqa: BLE001
        log.error("list users: %s", err)
        return error_response(500, "failed to list users")
    return json_response(users or [])


async def _get_user(request: web.Request) -> web.Response:
    svc = _svc(request)
    uid = request.match_info["id"]
    try:
        u = svc.store.get_user(uid)
    except Exception:  # noqa: BLE001
        return error_response(404, "user not found")
    return json_response(u)


async def _create_user(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or ""
    email = (body.get("email") or "").strip()

    if username == "" or password == "":
        return error_response(400, "username and password are required")
    if len(password) < MIN_PASSWORD_LENGTH:
        return error_response(400, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if not _valid_role(role):
        return error_response(400, "invalid role")

    try:
        pw_hash = await asyncio.to_thread(hash_password, password)
    except Exception as err:  # noqa: BLE001
        log.error("hash password: %s", err)
        return error_response(500, "failed to hash password")

    try:
        svc.store.get_user_by_username(username)
        return error_response(409, "a user with that username already exists")
    except Exception:  # noqa: BLE001 - not found: the name is free
        pass

    u = User(
        id=str(uuid.uuid4()),
        username=username,
        role=role,
        password_hash=pw_hash,
        email=email,
        authSource=AuthSourceLocal,
        createdAt=now_rfc3339(),
    )
    try:
        svc.store.create_user(u)
    except Exception as err:  # noqa: BLE001
        log.error("create user: %s username=%s", err, u.username)
        return error_response(500, "failed to create user")
    return json_response(u, status=201)


async def _update_user(request: web.Request) -> web.Response:
    svc = _svc(request)
    uid = request.match_info["id"]
    try:
        existing = svc.store.get_user(uid)
    except Exception:  # noqa: BLE001
        return error_response(404, "user not found")

    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    username = (body.get("username") or "").strip()
    if username != "":
        existing.username = username
    if "email" in body:
        existing.email = (body.get("email") or "").strip()
    role = body.get("role") or ""
    role_changed = False
    if role != "":
        if not _valid_role(role):
            return error_response(400, "invalid role")
        role_changed = role != existing.role
        existing.role = role
    password = body.get("password") or ""
    if password != "" and len(password) < MIN_PASSWORD_LENGTH:
        return error_response(400, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password != "":
        try:
            existing.password_hash = await asyncio.to_thread(hash_password, password)
        except Exception as err:  # noqa: BLE001
            log.error("hash password: %s", err)
            return error_response(500, "failed to hash password")

    try:
        svc.store.update_user(existing)
        if password != "" or role_changed:
            # A new password ends every session opened with the old one; a role
            # change makes the user sign in again under the new role (open
            # WebSockets included, once they reconnect).
            svc.store.bump_session_version(uid)
    except Exception as err:  # noqa: BLE001
        log.error("update user: %s id=%s", err, uid)
        return error_response(500, "failed to update user")
    resp = json_response(existing)
    cu = user_from(request)
    if cu is not None and cu.id == uid and (password != "" or role_changed):
        # Changing your own password should not sign you out of this tab: hand
        # back a token under the new session version.
        resp.headers["X-Session-Token"] = svc.jwt.issue(
            existing.id, existing.username, existing.role, svc.store.session_version(uid)
        )
    return resp


async def _delete_user(request: web.Request) -> web.Response:
    svc = _svc(request)
    uid = request.match_info["id"]

    # Prevent admins from deleting their own account, which would lock them out.
    cu = user_from(request)
    if cu is not None and cu.id == uid:
        return error_response(400, "cannot delete your own account")

    try:
        svc.store.get_user(uid)
    except Exception:  # noqa: BLE001
        return error_response(404, "user not found")

    try:
        svc.store.delete_user(uid)
        # Drop team memberships, direct grants and SSO identity links too.
        svc.store.delete_user_memberships(uid)
        svc.store.delete_user_identities(uid)
    except Exception as err:  # noqa: BLE001
        log.error("delete user: %s id=%s", err, uid)
        return error_response(500, "failed to delete user")
    return json_response({"status": "deleted"})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _record_login(svc, request: web.Request, action: str, user_id: str, username: str, status: int) -> None:
    """Record an authentication event (login/login_failed) via the audit
    recorder when one is configured. Never blocks the login response. The store
    stamps the Time field itself."""
    rec = getattr(svc, "audit", None)
    if rec is None:
        return
    entry = {
        "id": str(uuid.uuid4()),
        "userId": user_id,
        "username": username,
        "action": action,
        "path": request.rel_url.path,
        "status": status,
        "ip": _client_ip(request),
    }
    try:
        result = rec.record(entry)
        if result is not None and hasattr(result, "__await__"):
            # Fire-and-forget: recording must not delay the login response.
            import asyncio

            asyncio.ensure_future(_await_quietly(result))
    except Exception as err:  # noqa: BLE001
        log.error("audit: record login event: %s action=%s username=%s", err, action, username)


async def _await_quietly(awaitable) -> None:
    try:
        await awaitable
    except Exception as err:  # noqa: BLE001
        log.error("audit: record login event (async): %s", err)


def _client_ip(request: web.Request) -> str:
    """The client's address (proxy headers only from trusted proxies)."""
    return client_ip(request)
