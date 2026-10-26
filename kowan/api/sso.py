"""Single sign-on HTTP API (OpenID Connect + GitHub). See docs/sso.md.

Public (login page / browser redirects):

  GET  /api/v1/auth/sso/providers          enabled providers + ``ssoOnly``
  GET  /api/v1/auth/sso/{id}/start         302 to the IdP (sets the state cookie)
  GET  /api/v1/auth/sso/{id}/callback      IdP redirect target; on success sets a
                                           one-time ticket cookie and 302s to
                                           ``/login/sso`` (no token in any URL)
  POST /api/v1/auth/sso/exchange           ticket cookie -> ``{token, user, returnTo}``

Admin:

  GET/POST          /sso/providers
  GET/PUT/DELETE    /sso/providers/{id}
  POST              /sso/providers/{id}/test   (discovery + JWKS reachability)
  GET/PUT           /sso/settings              ``{ssoOnly, breakGlassUser}``

Env bootstrap (applied at startup, providers marked ``managed: env``):
``KOWAN_SSO_PROVIDERS`` (JSON array of provider objects, ``clientSecret`` may be
given as ``clientSecretEnv`` naming another variable), ``KOWAN_SSO_ONLY`` and
``KOWAN_SSO_BREAK_GLASS_USER``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from aiohttp import web

from kowan.authn import mapping
from kowan.authn.github import DEFAULT_SCOPES as GITHUB_SCOPES
from kowan.authn.github import GitHubClient
from kowan.authn.oidc import OIDCClient, OIDCError, code_challenge, consteq, new_code_verifier, new_token
from kowan.authn.provision import ProvisionError, provision
from kowan.authn.state import (
    COOKIE_PATH,
    STATE_COOKIE,
    STATE_TTL,
    TICKET_COOKIE,
    open_sealed,
    safe_return_to,
    seal,
)
from kowan.store._common import MASK_VALUE
from kowan.store.sso import SSOProvider
from kowan.web import (
    SERVICES_KEY,
    add_public_route,
    api_route,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    require_role,
    user_from,
)

log = logging.getLogger("kowan.api.sso")

TYPE_OIDC = "oidc"
TYPE_GITHUB = "github"
TYPES = (TYPE_OIDC, TYPE_GITHUB)
DEFAULT_OIDC_SCOPES = "openid profile email"
TICKET_TTL = 60
SETTING_SSO_ONLY = "sso_only"
SETTING_BREAK_GLASS = "sso_break_glass_user"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_CONFIG_KEYS = {
    "defaultRole", "roleMappings", "teamMappings", "allowSignup", "linkByEmail", "trustEmail",
    "allowedDomains", "useUserinfo", "extraAuthParams", "tokenAuthMethod", "baseUrl", "apiUrl",
}

# Tests inject an httpx client factory (with a MockTransport) here.
HTTP_FACTORY = None


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


# ---------------------------------------------------------------------------
# settings helpers (also used by authapi for the SSO-only password gate)
# ---------------------------------------------------------------------------


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def sso_only(svc: Any) -> bool:
    try:
        return _truthy(svc.store.get_setting(SETTING_SSO_ONLY))
    except Exception:  # noqa: BLE001
        return False


def break_glass_user(svc: Any) -> str:
    try:
        v = svc.store.get_setting(SETTING_BREAK_GLASS)
    except Exception:  # noqa: BLE001
        v = ""
    return v or getattr(getattr(svc, "config", None), "admin_user", "") or "admin"


def password_login_allowed(svc: Any, user: Any) -> bool:
    """In SSO-only mode only the break-glass local admin may use a password."""
    if not sso_only(svc):
        return True
    if user is None:
        return False
    return (
        user.username == break_glass_user(svc)
        and user.role == "admin"
        and (user.auth_source or "local") == "local"
    )


def redirect_uri(svc: Any, provider_id: str) -> str:
    base = (getattr(getattr(svc, "config", None), "external_url", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/api/v1/auth/sso/{provider_id}/callback"


def _secure_cookies(svc: Any) -> bool:
    return (getattr(getattr(svc, "config", None), "external_url", "") or "").startswith("https://")


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------


def _clients(svc: Any) -> Dict[str, Tuple[str, Any]]:
    try:
        return svc.sso_clients
    except AttributeError:
        cache: Dict[str, Tuple[str, Any]] = {}
        svc.attach("sso_clients", cache)
        return cache


def _client_for(svc: Any, p: SSOProvider) -> Any:
    fp = hashlib.sha256(
        json.dumps([p.type, p.issuer, p.client_id, p.client_secret, p.updated_at,
                    p.config.get("baseUrl"), p.config.get("apiUrl"), p.config.get("tokenAuthMethod")]).encode()
    ).hexdigest()
    cache = _clients(svc)
    hit = cache.get(p.id)
    if hit is not None and hit[0] == fp:
        return hit[1]
    if p.type == TYPE_GITHUB:
        client: Any = GitHubClient(p.client_id, p.client_secret, base_url=p.config.get("baseUrl", ""),
                                   api_url=p.config.get("apiUrl", ""), http=HTTP_FACTORY)
    else:
        client = OIDCClient(p.issuer, p.client_id, p.client_secret, http=HTTP_FACTORY,
                            token_auth_method=str(p.config.get("tokenAuthMethod") or ""))
    cache[p.id] = (fp, client)
    return client


def _scopes(p: SSOProvider) -> list:
    raw = p.scopes or (" ".join(GITHUB_SCOPES) if p.type == TYPE_GITHUB else DEFAULT_OIDC_SCOPES)
    scopes = [s for s in re.split(r"[\s,]+", raw) if s]
    if p.type == TYPE_OIDC and "openid" not in scopes:
        scopes.insert(0, "openid")
    return scopes


# ---------------------------------------------------------------------------
# public: provider list, start, callback, exchange
# ---------------------------------------------------------------------------


async def _public_providers(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        rows = [p for p in svc.store.list_sso_providers() if p.enabled]
    except Exception as err:  # noqa: BLE001
        log.error("sso: list providers: %s", err)
        rows = []
    return json_response({
        "providers": [{"id": p.id, "name": p.name, "type": p.type, "label": p.label or p.name, "icon": p.icon}
                      for p in rows],
        "ssoOnly": sso_only(svc) and bool(rows),
    })


def _login_error(code: str) -> web.Response:
    resp = web.HTTPFound("/login?" + urlencode({"sso_error": code}))
    resp.del_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


def _load_enabled(svc: Any, provider_id: str) -> Optional[SSOProvider]:
    try:
        p = svc.store.get_sso_provider(provider_id, with_secret=True)
    except Exception:  # noqa: BLE001
        return None
    return p if p.enabled else None


async def _start(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    p = _load_enabled(svc, request.match_info["id"])
    if p is None:
        return _login_error("unknown_provider")
    redirect = redirect_uri(svc, p.id)
    if not redirect:
        log.error("sso: KOWAN_EXTERNAL_URL is not set; cannot build the redirect URI")
        return _login_error("not_configured")
    state, nonce, verifier = new_token(), new_token(), new_code_verifier()
    try:
        if p.type == TYPE_GITHUB:
            url = _client_for(svc, p).authorization_url(redirect, _scopes(p), state, code_challenge(verifier))
        else:
            extra = {str(k): str(v) for k, v in (p.config.get("extraAuthParams") or {}).items()
                     if k not in ("state", "nonce", "redirect_uri", "client_id", "code_challenge",
                                  "code_challenge_method", "response_type", "scope")}
            url = await _client_for(svc, p).authorization_url(
                redirect, _scopes(p), state, nonce, code_challenge(verifier), extra)
    except OIDCError as err:
        log.warning("sso: start %s failed: %s", p.name, err)
        return _login_error(err.code)
    sealed = seal(svc.cipher, {
        "p": p.id, "s": state, "n": nonce, "v": verifier,
        "r": safe_return_to(request.query.get("returnTo")), "exp": time.time() + STATE_TTL,
    })
    resp = web.HTTPFound(url)
    resp.set_cookie(STATE_COOKIE, sealed, path=COOKIE_PATH, max_age=STATE_TTL, httponly=True,
                    samesite="Lax", secure=_secure_cookies(svc))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _tickets(svc: Any) -> Dict[str, Tuple[str, str, float]]:
    try:
        return svc.sso_tickets
    except AttributeError:
        t: Dict[str, Tuple[str, str, float]] = {}
        svc.attach("sso_tickets", t)
        return t


async def _callback(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    provider_id = request.match_info["id"]
    st = open_sealed(svc.cipher, request.cookies.get(STATE_COOKIE, ""))
    if st is None or st.get("p") != provider_id:
        return _login_error("state_expired")
    if request.query.get("error"):
        code = re.sub(r"[^a-z_]", "", str(request.query.get("error")).lower())[:40] or "denied"
        return _login_error("idp_" + code)
    if not consteq(request.query.get("state", ""), str(st.get("s", ""))):
        return _login_error("state_mismatch")
    code = request.query.get("code", "")
    if not code:
        return _login_error("missing_code")
    p = _load_enabled(svc, provider_id)
    if p is None:
        return _login_error("unknown_provider")
    redirect = redirect_uri(svc, p.id)
    client = _client_for(svc, p)
    try:
        if p.type == TYPE_GITHUB:
            access = await client.exchange_code(code, redirect, st["v"])
            claims = await client.identity(access, want_groups=True)
        else:
            tokens = await client.exchange_code(code, redirect, st["v"])
            id_token = tokens.get("id_token")
            if not id_token:
                raise OIDCError("token response has no id_token", "invalid_id_token")
            claims = await client.validate_id_token(str(id_token), str(st.get("n", "")))
            access = tokens.get("access_token")
            if access and (p.config.get("useUserinfo") or not claims.get("email")):
                try:
                    info = await client.userinfo(str(access))
                except OIDCError as err:
                    log.info("sso: userinfo %s failed: %s", p.name, err)
                    info = {}
                if info.get("sub") == claims.get("sub"):
                    for k, v in info.items():
                        if k not in ("sub", "iss", "aud", "exp", "iat", "nonce"):
                            claims.setdefault(k, v)
        user, created = await asyncio.to_thread(provision, svc.store, p, claims, now_rfc3339())
    except OIDCError as err:
        log.warning("sso: login via %s failed: %s", p.name, err)
        _audit(svc, request, "login_failed", "", f"sso:{p.name}", 401, err.code)
        return _login_error(err.code)
    except ProvisionError as err:
        log.warning("sso: login via %s refused: %s", p.name, err)
        _audit(svc, request, "login_failed", "", f"sso:{p.name}", 403, err.code)
        return _login_error(err.code)

    ticket = new_token()
    tickets = _tickets(svc)
    now = time.time()
    for k in [k for k, v in tickets.items() if v[2] < now]:
        tickets.pop(k, None)
    tickets[hashlib.sha256(ticket.encode()).hexdigest()] = (user.id, str(st.get("r") or "/"), now + TICKET_TTL)
    _audit(svc, request, "login_sso", user.id, user.username, 200,
           f"provider={p.name}" + (" provisioned" if created else ""))
    resp = web.HTTPFound("/login/sso")
    resp.del_cookie(STATE_COOKIE, path=COOKIE_PATH)
    resp.set_cookie(TICKET_COOKIE, ticket, path=COOKIE_PATH, max_age=TICKET_TTL, httponly=True,
                    samesite="Lax", secure=_secure_cookies(svc))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


async def _exchange(request: web.Request) -> web.Response:
    svc = _svc(request)
    ticket = request.cookies.get(TICKET_COOKIE, "")
    entry = _tickets(svc).pop(hashlib.sha256(ticket.encode()).hexdigest(), None) if ticket else None
    if entry is None or entry[2] < time.time():
        resp = error_response(401, "sign-in expired; start again")
        resp.del_cookie(TICKET_COOKIE, path=COOKIE_PATH)
        return resp
    try:
        u = svc.store.get_user(entry[0])
        token = svc.jwt.issue(u.id, u.username, u.role, svc.store.session_version(u.id))
    except Exception as err:  # noqa: BLE001
        log.error("sso: exchange: %s", err)
        return error_response(500, "failed to issue token")
    resp = json_response({"token": token, "user": u, "returnTo": entry[1]})
    resp.del_cookie(TICKET_COOKIE, path=COOKIE_PATH)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _audit(svc: Any, request: web.Request, action: str, user_id: str, username: str, status: int, detail: str = "") -> None:
    rec = getattr(svc, "audit", None)
    if rec is None:
        return
    from kowan.web import client_ip

    try:
        rec.record({"id": str(uuid.uuid4()), "userId": user_id, "username": username, "action": action,
                    "path": request.rel_url.path, "status": status, "ip": client_ip(request), "detail": detail})
    except Exception as err:  # noqa: BLE001
        log.error("sso: audit: %s", err)


# ---------------------------------------------------------------------------
# admin CRUD
# ---------------------------------------------------------------------------


def _provider_out(svc: Any, p: SSOProvider) -> dict:
    d = p.to_dict()
    d["redirectUri"] = redirect_uri(svc, p.id)
    return d


def _apply_body(p: SSOProvider, body: dict, *, creating: bool) -> Optional[str]:
    """Validate and copy an admin request body onto ``p``; returns an error."""
    allowed = {"name", "type", "label", "icon", "issuer", "clientId", "clientSecret", "scopes", "config", "enabled"}
    unknown = set(body) - allowed
    if unknown:
        return "unknown field: " + ", ".join(sorted(unknown))
    if "name" in body or creating:
        p.name = str(body.get("name", "")).strip().lower()
        if not _NAME_RE.match(p.name):
            return "name must be 1-40 lowercase letters, digits or dashes"
    if "type" in body or creating:
        p.type = str(body.get("type") or TYPE_OIDC)
        if p.type not in TYPES:
            return "type must be oidc or github"
    for key, attr in (("label", "label"), ("icon", "icon"), ("issuer", "issuer"), ("clientId", "client_id"),
                      ("scopes", "scopes")):
        if key in body:
            setattr(p, attr, str(body.get(key) or "").strip())
    if "enabled" in body:
        p.enabled = bool(body.get("enabled"))
    if "config" in body:
        cfg = body.get("config") or {}
        if not isinstance(cfg, dict):
            return "config must be an object"
        bad = set(cfg) - _CONFIG_KEYS
        if bad:
            return "unknown config key: " + ", ".join(sorted(bad))
        try:
            cfg["roleMappings"] = mapping.validate_rules(cfg.get("roleMappings") or [], "role")
            cfg["teamMappings"] = mapping.validate_rules(cfg.get("teamMappings") or [], "team")
        except ValueError as err:
            return str(err)
        if cfg.get("defaultRole", "viewer") not in mapping.ROLES_RANK:
            return "invalid defaultRole"
        if not isinstance(cfg.get("allowedDomains", []), list):
            return "allowedDomains must be a list"
        if not isinstance(cfg.get("extraAuthParams", {}), dict):
            return "extraAuthParams must be an object"
        p.config = cfg
    if p.type == TYPE_OIDC and not re.match(r"^https?://", p.issuer or ""):
        return "issuer must be an http(s) URL"
    if p.type == TYPE_OIDC and p.issuer.startswith("http://") and not re.match(
            r"^http://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)", p.issuer):
        return "issuer must use https (http is only allowed for localhost)"
    if not p.client_id:
        return "clientId is required"
    return None


async def _list(request: web.Request) -> web.Response:
    svc = _svc(request)
    return json_response({
        "providers": [_provider_out(svc, p) for p in svc.store.list_sso_providers()],
        "externalUrl": getattr(svc.config, "external_url", "") or "",
        "settings": _settings_out(svc),
    })


async def _get(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        p = svc.store.get_sso_provider(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "provider not found")
    return json_response(_provider_out(svc, p))


async def _create(request: web.Request) -> web.Response:
    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    now = now_rfc3339()
    p = SSOProvider(id=str(uuid.uuid4()), name="", type=TYPE_OIDC, created_at=now, updated_at=now)
    err = _apply_body(p, body, creating=True)
    if err:
        return error_response(400, err)
    secret = str(body.get("clientSecret") or "")
    p.client_secret = "" if secret == MASK_VALUE else secret
    if p.type == TYPE_GITHUB and not p.client_secret:
        return error_response(400, "clientSecret is required for GitHub")
    try:
        svc.store.get_sso_provider_by_name(p.name)
        return error_response(409, "a provider with that name already exists")
    except Exception:  # noqa: BLE001
        pass
    svc.store.create_sso_provider(p)
    _audit(svc, request, "sso.provider.create", _uid(request), _uname(request), 201, f"provider={p.name}")
    return json_response(_provider_out(svc, svc.store.get_sso_provider(p.id)), status=201)


async def _update(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        p = svc.store.get_sso_provider(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "provider not found")
    body = await read_json(request)
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    old_name = p.name
    err = _apply_body(p, body, creating=False)
    if err:
        return error_response(400, err)
    if p.name != old_name:
        try:
            svc.store.get_sso_provider_by_name(p.name)
            return error_response(409, "a provider with that name already exists")
        except Exception:  # noqa: BLE001
            pass
    secret = body.get("clientSecret")
    keep = secret is None or secret == MASK_VALUE
    if not keep:
        p.client_secret = str(secret or "")
    p.updated_at = now_rfc3339()
    svc.store.update_sso_provider(p, keep_secret=keep)
    _clients(svc).pop(p.id, None)
    _audit(svc, request, "sso.provider.update", _uid(request), _uname(request), 200, f"provider={p.name}")
    return json_response(_provider_out(svc, svc.store.get_sso_provider(p.id)))


async def _delete(request: web.Request) -> web.Response:
    svc = _svc(request)
    pid = request.match_info["id"]
    try:
        p = svc.store.get_sso_provider(pid)
    except Exception:  # noqa: BLE001
        return error_response(404, "provider not found")
    svc.store.delete_sso_provider(pid)
    _clients(svc).pop(pid, None)
    _audit(svc, request, "sso.provider.delete", _uid(request), _uname(request), 200, f"provider={p.name}")
    return json_response({"status": "deleted"})


async def _test(request: web.Request) -> web.Response:
    """Check discovery + JWKS (OIDC) so a typo shows up before users try it."""
    svc = _svc(request)
    try:
        p = svc.store.get_sso_provider(request.match_info["id"], with_secret=True)
    except Exception:  # noqa: BLE001
        return error_response(404, "provider not found")
    if p.type == TYPE_GITHUB:
        return json_response({"ok": True, "detail": "GitHub uses fixed endpoints; nothing to discover",
                              "redirectUri": redirect_uri(svc, p.id)})
    client = _client_for(svc, p)
    try:
        d = await client.discovery()
        from kowan.authn.oidc import get_json

        jwks = await get_json(client._http, d.jwks_uri)  # noqa: SLF001
        n = len(jwks.get("keys", [])) if isinstance(jwks, dict) else 0
    except OIDCError as err:
        return json_response({"ok": False, "detail": str(err), "redirectUri": redirect_uri(svc, p.id)})
    return json_response({"ok": True, "detail": f"issuer {d.issuer}; {n} signing key(s)",
                          "authorizationEndpoint": d.authorization_endpoint,
                          "redirectUri": redirect_uri(svc, p.id)})


def _settings_out(svc: Any) -> dict:
    return {"ssoOnly": sso_only(svc), "breakGlassUser": break_glass_user(svc)}


async def _get_settings(request: web.Request) -> web.Response:
    return json_response(_settings_out(_svc(request)))


async def _put_settings(request: web.Request) -> web.Response:
    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict) or set(body) - {"ssoOnly", "breakGlassUser"}:
        return error_response(400, "invalid request body")
    if "breakGlassUser" in body:
        name = str(body.get("breakGlassUser") or "").strip()
        if name:
            try:
                u = svc.store.get_user_by_username(name)
            except Exception:  # noqa: BLE001
                return error_response(400, "break-glass user not found")
            if u.role != "admin" or (u.auth_source or "local") != "local" or not u.password_hash:
                return error_response(400, "break-glass user must be a local admin with a password")
        svc.store.set_setting(SETTING_BREAK_GLASS, name)
    if "ssoOnly" in body:
        on = bool(body.get("ssoOnly"))
        if on:
            bg = break_glass_user(svc)
            try:
                u = svc.store.get_user_by_username(bg)
                ok = u.role == "admin" and (u.auth_source or "local") == "local" and bool(u.password_hash)
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                return error_response(400, "set a break-glass local admin before enabling SSO-only mode")
            if not any(p.enabled for p in svc.store.list_sso_providers()):
                return error_response(400, "enable at least one SSO provider first")
        svc.store.set_setting(SETTING_SSO_ONLY, "true" if on else "false")
    _audit(svc, request, "sso.settings.update", _uid(request), _uname(request), 200, json.dumps(_settings_out(svc)))
    return json_response(_settings_out(svc))


def _uid(request: web.Request) -> str:
    u = user_from(request)
    return getattr(u, "id", "") if u is not None else ""


def _uname(request: web.Request) -> str:
    u = user_from(request)
    return getattr(u, "username", "") if u is not None else ""


# ---------------------------------------------------------------------------
# env bootstrap
# ---------------------------------------------------------------------------


def bootstrap_from_env(svc: Any, environ: Optional[dict] = None) -> None:
    env = os.environ if environ is None else environ
    raw = (env.get("KOWAN_SSO_PROVIDERS") or "").strip()
    if raw:
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("expected a JSON array")
        except ValueError as err:
            log.error("sso: KOWAN_SSO_PROVIDERS is not valid JSON: %s", err)
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            secret_env = item.pop("clientSecretEnv", None)
            if secret_env:
                item["clientSecret"] = env.get(str(secret_env), "")
            secret = str(item.pop("clientSecret", "") or "")
            now = now_rfc3339()
            try:
                existing = svc.store.get_sso_provider_by_name(str(item.get("name", "")).strip().lower())
            except Exception:  # noqa: BLE001
                existing = None
            p = existing or SSOProvider(id=str(uuid.uuid4()), name="", type=TYPE_OIDC, created_at=now)
            err = _apply_body(p, item, creating=existing is None)
            if err:
                log.error("sso: env provider %r skipped: %s", item.get("name"), err)
                continue
            p.managed = "env"
            p.updated_at = now
            if existing is None:
                p.client_secret = secret
                svc.store.create_sso_provider(p)
                log.info("sso: bootstrapped provider %r from env", p.name)
            else:
                p.client_secret = secret
                svc.store.update_sso_provider(p, keep_secret=not secret)
    if env.get("KOWAN_SSO_BREAK_GLASS_USER"):
        svc.store.set_setting(SETTING_BREAK_GLASS, env["KOWAN_SSO_BREAK_GLASS_USER"].strip())
    if env.get("KOWAN_SSO_ONLY") is not None and env.get("KOWAN_SSO_ONLY") != "":
        svc.store.set_setting(SETTING_SSO_ONLY, "true" if _truthy(env.get("KOWAN_SSO_ONLY")) else "false")


def register(app: web.Application, svc: Any) -> None:
    add_public_route(app, "GET", "/api/v1/auth/sso/providers", _public_providers)
    add_public_route(app, "GET", "/api/v1/auth/sso/{id}/start", _start)
    add_public_route(app, "GET", "/api/v1/auth/sso/{id}/callback", _callback)
    add_public_route(app, "POST", "/api/v1/auth/sso/exchange", _exchange)

    admin = require_role("admin")
    api_route(app, "GET", "/sso/providers", admin(_list))
    api_route(app, "POST", "/sso/providers", admin(_create))
    api_route(app, "GET", "/sso/providers/{id}", admin(_get))
    api_route(app, "PUT", "/sso/providers/{id}", admin(_update))
    api_route(app, "DELETE", "/sso/providers/{id}", admin(_delete))
    api_route(app, "POST", "/sso/providers/{id}/test", admin(_test))
    api_route(app, "GET", "/sso/settings", admin(_get_settings))
    api_route(app, "PUT", "/sso/settings", admin(_put_settings))

    if getattr(svc, "store", None) is not None and getattr(svc, "cipher", None) is not None:
        try:
            bootstrap_from_env(svc)
        except Exception as err:  # noqa: BLE001
            log.error("sso: env bootstrap failed: %s", err)
