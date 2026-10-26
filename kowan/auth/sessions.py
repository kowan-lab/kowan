"""Server-side check of a JWT against the database, on every request.

A valid signature is not enough: the user must still exist, the token must not
be revoked (logout), and it must carry the user's current session version
(bumped on password or role change). The role and username come from the
database, never from the token, so a demotion takes effect immediately.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

from .jwt import Claims

log = logging.getLogger("kowan.auth.sessions")

# (role, username) of the live user, or None when the session is no longer valid.
SessionChecker = Callable[[Claims], Optional[Tuple[str, str]]]


def make_session_checker(store: Any) -> SessionChecker:
    def check(claims: Claims) -> Optional[Tuple[str, str]]:
        if not claims.sub or not claims.jti:
            return None
        try:
            user = store.get_user(claims.sub)
        except Exception:  # noqa: BLE001 - deleted user (NotFoundError) or store error
            return None
        try:
            if claims.ver != store.session_version(claims.sub):
                return None
            if store.is_token_revoked(claims.jti):
                return None
        except Exception as err:  # noqa: BLE001
            log.error("session check failed: %s", err)
            return None
        return user.role, user.username

    return check
