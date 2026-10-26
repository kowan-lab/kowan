"""Just-in-time provisioning and account linking for SSO logins.

Order of resolution for a validated identity ``(provider, sub)``:

1. an existing link in ``user_identities`` -> that user;
2. else, when the provider asserts a **verified** email and ``linkByEmail`` is
   on (default), the local user with that email (case-insensitive) is linked;
3. else, when ``allowSignup`` is on (default), a new user is created with the
   mapped role (``defaultRole`` + ``roleMappings``) and no password.

On every login SSO-provisioned users get their role re-synced from the claims
when ``roleMappings`` are configured (linked local accounts keep their role so
a break-glass admin can never be demoted by an IdP), and team memberships made
by this provider are re-synced from ``teamMappings`` (manual memberships are
never touched). Unverified emails are never stored, so they cannot be used to
hijack a later link.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Tuple

from kowan.models import User

from .mapping import map_role, map_teams

log = logging.getLogger("kowan.authn.provision")

AUTH_SOURCE_PREFIX = "sso:"


class ProvisionError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _username_base(claims: Dict[str, Any]) -> str:
    for key in ("preferred_username", "login", "nickname", "email", "name"):
        v = str(claims.get(key) or "").strip()
        if v:
            if key == "email":
                v = v.split("@", 1)[0]
            v = re.sub(r"[^A-Za-z0-9._@-]+", "-", v).strip("-.")[:64]
            if v:
                return v
    return "user"


def _unique_username(store: Any, base: str) -> str:
    name = base
    for i in range(2, 1000):
        try:
            store.get_user_by_username(name)
        except Exception:  # noqa: BLE001 - NotFoundError => free
            return name
        name = f"{base}-{i}"
    return f"{base}-{uuid.uuid4().hex[:8]}"


def resolve_team_ids(store: Any, refs: List[str]) -> List[str]:
    ids: List[str] = []
    for ref in refs:
        team = None
        try:
            team = store.get_team(ref)
        except Exception:  # noqa: BLE001
            try:
                team = store.get_team_by_name(ref)
            except Exception:  # noqa: BLE001
                log.warning("sso: team mapping refers to unknown team %r", ref)
        if team is not None and team.id not in ids:
            ids.append(team.id)
    return ids


def provision(store: Any, provider: Any, claims: Dict[str, Any], now: str) -> Tuple[User, bool]:
    cfg: Dict[str, Any] = provider.config or {}
    subject = str(claims.get("sub") or "")
    if not subject:
        raise ProvisionError("identity has no subject", "invalid_identity")
    email = str(claims.get("email") or "").strip()
    verified = bool(email) and (_truthy(claims.get("email_verified")) or _truthy(cfg.get("trustEmail")))

    domains = [str(d).lower().lstrip("@") for d in cfg.get("allowedDomains") or [] if str(d).strip()]
    if domains:
        dom = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if not verified or dom not in domains:
            raise ProvisionError("email domain not allowed", "domain_not_allowed")

    role = map_role(claims, cfg)
    created = False
    user = None
    try:
        user = store.get_user(store.get_identity_user_id(provider.id, subject))
        store.touch_identity(provider.id, subject, email if verified else "", now)
    except Exception:  # noqa: BLE001 - no link yet (or a dangling one)
        user = None
    if user is None:
        if verified and cfg.get("linkByEmail", True):
            try:
                user = store.get_user_by_email(email)
            except Exception:  # noqa: BLE001
                user = None
        if user is None:
            if not cfg.get("allowSignup", True):
                raise ProvisionError("no account for this identity and sign-up is disabled", "signup_disabled")
            user = User(
                id=str(uuid.uuid4()),
                username=_unique_username(store, _username_base(claims)),
                role=role,
                passwordHash="",
                email=email if verified else "",
                authSource=AUTH_SOURCE_PREFIX + provider.name,
                createdAt=now,
            )
            store.create_user(user)
            created = True
            log.info("sso: provisioned user %r via %s", user.username, provider.name)
        store.link_identity(provider.id, subject, user.id, email if verified else "", now)

    if (not created and (user.auth_source or "").startswith(AUTH_SOURCE_PREFIX)
            and cfg.get("roleMappings") and user.role != role):
        user.role = role
        store.update_user(user)

    if cfg.get("teamMappings"):
        team_ids = resolve_team_ids(store, map_teams(claims, cfg))
        store.sync_sso_memberships(user.id, AUTH_SOURCE_PREFIX + provider.id, team_ids, now)
    return user, created
