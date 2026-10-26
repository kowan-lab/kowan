"""Session revocation for issued JWTs.

Provides :class:`_SessionMixin`, folded into :class:`kowan.store.store.Store`.

Two levers, both checked on every request by :mod:`kowan.auth.sessions`:

* a per-user ``version`` stamped into each token: bumping it ends every session
  of that user (password or role change);
* a deny-list of single token ids (``jti``), for logout of one session. Rows are
  kept only until the token would have expired anyway.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


class _SessionMixin(_Base):
    def session_version(self, user_id: str) -> int:
        row = self._one("SELECT version FROM user_sessions WHERE user_id = ?", (user_id,))
        return int(row["version"]) if row is not None else 0

    def bump_session_version(self, user_id: str) -> int:
        """End every session of ``user_id``; returns the new version."""
        self._execute(
            "INSERT INTO user_sessions (user_id, version) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET version = version + 1",
            (user_id,),
        )
        return self.session_version(user_id)

    def revoke_token(self, jti: str, exp: int) -> None:
        now = int(time.time())
        self._execute("DELETE FROM revoked_tokens WHERE exp <= ?", (now,))
        self._execute(
            "INSERT OR IGNORE INTO revoked_tokens (jti, exp) VALUES (?, ?)", (jti, int(exp))
        )

    def is_token_revoked(self, jti: str) -> bool:
        return self._one("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)) is not None
