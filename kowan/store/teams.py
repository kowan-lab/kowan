"""Teams, team membership and resource-scoped permission grants.

Provides :class:`_TeamMixin`, folded into :class:`kowan.store.store.Store`
(the tables are part of ``_SCHEMA`` in kowan/store/store.py).

Model (see docs/permissions.md):

* ``teams``         a named group of users with a description.
* ``team_members``  (team, user) pairs; ``source`` is ``manual`` for admin-made
                    memberships or ``sso:<provider id>`` for memberships kept in
                    sync from an identity provider's claims.
* ``grants``        subject (team | user | apikey) x scope x level. The scope is
                    ``all_hosts`` | ``host`` | ``deployment`` | ``stack`` |
                    ``labels``; ``host_id`` and ``target`` qualify it (target is
                    the deployment id, the compose project or ``key=value``).

Authorization reads go through :meth:`_TeamMixin.authz_snapshot`, an in-memory
copy of memberships + grants rebuilt only after a write (the tables are tiny and
read on every authenticated request), so an install without teams pays one
dict lookup per request.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ._common import NotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


SUBJECT_TEAM = "team"
SUBJECT_USER = "user"
SUBJECT_APIKEY = "apikey"
SUBJECT_TYPES = (SUBJECT_TEAM, SUBJECT_USER, SUBJECT_APIKEY)

SOURCE_MANUAL = "manual"


@dataclass
class Team:
    id: str
    name: str
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    members: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "members": self.members,
        }


@dataclass
class GrantRow:
    id: str
    subject_type: str
    subject_id: str
    scope_type: str
    host_id: str
    target: str
    level: str
    created_at: str = ""
    created_by: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subjectType": self.subject_type,
            "subjectId": self.subject_id,
            "scope": self.scope_type,
            "hostId": self.host_id,
            "target": self.target,
            "level": self.level,
            "createdAt": self.created_at,
            "createdBy": self.created_by,
        }


@dataclass
class AuthzSnapshot:
    """Immutable view of memberships + grants used by kowan.auth.authorize."""

    generation: int
    # user id -> team ids
    memberships: Dict[str, Tuple[str, ...]]
    # (subject_type, subject_id) -> grants
    grants: Dict[Tuple[str, str], Tuple[GrantRow, ...]]

    @property
    def empty(self) -> bool:
        return not self.memberships and not self.grants


def _row_to_grant(r: sqlite3.Row) -> GrantRow:
    return GrantRow(
        id=r["id"],
        subject_type=r["subject_type"],
        subject_id=r["subject_id"],
        scope_type=r["scope_type"],
        host_id=r["host_id"],
        target=r["target"],
        level=r["level"],
        created_at=r["created_at"],
        created_by=r["created_by"],
    )


class _TeamMixin(_Base):
    # Cache of the authz snapshot; rebuilt lazily after any write here.
    _authz_cache: Optional[AuthzSnapshot] = None
    _authz_gen: int = 0

    def _authz_invalidate(self) -> None:
        self._authz_gen = (self._authz_gen or 0) + 1
        self._authz_cache = None

    # -- teams ---------------------------------------------------------------

    def create_team(self, t: Team) -> None:
        self._execute(
            "INSERT INTO teams (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (t.id, t.name, t.description, t.created_at, t.updated_at),
        )
        self._authz_invalidate()

    def update_team(self, t: Team) -> None:
        self._execute(
            "UPDATE teams SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (t.name, t.description, t.updated_at, t.id),
        )

    def get_team(self, team_id: str) -> Team:
        row = self._one("SELECT * FROM teams WHERE id = ?", (team_id,))
        if row is None:
            raise NotFoundError("get team: no rows in result set")
        t = Team(row["id"], row["name"], row["description"], row["created_at"], row["updated_at"])
        t.members = self.list_team_members(t.id)
        return t

    def get_team_by_name(self, name: str) -> Team:
        row = self._one("SELECT id FROM teams WHERE lower(name) = lower(?)", (name,))
        if row is None:
            raise NotFoundError("get team: no rows in result set")
        return self.get_team(row["id"])

    def list_teams(self) -> List[Team]:
        rows = self._query("SELECT * FROM teams ORDER BY lower(name)")
        members = self._query(
            "SELECT m.team_id, m.user_id, m.source, m.added_at, COALESCE(u.username, '') AS username "
            "FROM team_members m LEFT JOIN users u ON u.id = m.user_id ORDER BY lower(u.username)"
        )
        by_team: Dict[str, List[dict]] = {}
        for m in members:
            by_team.setdefault(m["team_id"], []).append(
                {"userId": m["user_id"], "username": m["username"], "source": m["source"], "addedAt": m["added_at"]}
            )
        out = []
        for r in rows:
            t = Team(r["id"], r["name"], r["description"], r["created_at"], r["updated_at"])
            t.members = by_team.get(t.id, [])
            out.append(t)
        return out

    def delete_team(self, team_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
            self._execute(
                "DELETE FROM grants WHERE subject_type = ? AND subject_id = ?", (SUBJECT_TEAM, team_id)
            )
            self._execute("DELETE FROM teams WHERE id = ?", (team_id,))
        self._authz_invalidate()

    # -- membership ----------------------------------------------------------

    def list_team_members(self, team_id: str) -> List[dict]:
        rows = self._query(
            "SELECT m.user_id, m.source, m.added_at, COALESCE(u.username, '') AS username "
            "FROM team_members m LEFT JOIN users u ON u.id = m.user_id "
            "WHERE m.team_id = ? ORDER BY lower(u.username)",
            (team_id,),
        )
        return [
            {"userId": r["user_id"], "username": r["username"], "source": r["source"], "addedAt": r["added_at"]}
            for r in rows
        ]

    def add_team_member(self, team_id: str, user_id: str, source: str, added_at: str) -> bool:
        """Add a membership; returns False when it already existed. An existing
        manual membership is never downgraded to an SSO-synced one."""
        with self._lock:
            row = self._one(
                "SELECT source FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id)
            )
            if row is not None:
                return False
            self._execute(
                "INSERT INTO team_members (team_id, user_id, source, added_at) VALUES (?, ?, ?, ?)",
                (team_id, user_id, source or SOURCE_MANUAL, added_at),
            )
        self._authz_invalidate()
        return True

    def remove_team_member(self, team_id: str, user_id: str) -> None:
        self._execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id))
        self._authz_invalidate()

    def user_memberships(self, user_id: str) -> List[dict]:
        rows = self._query(
            "SELECT m.team_id, m.source, t.name FROM team_members m JOIN teams t ON t.id = m.team_id "
            "WHERE m.user_id = ? ORDER BY lower(t.name)",
            (user_id,),
        )
        return [{"teamId": r["team_id"], "teamName": r["name"], "source": r["source"]} for r in rows]

    def sync_sso_memberships(self, user_id: str, source: str, team_ids: List[str], now: str) -> None:
        """Make the user's ``source``-synced memberships exactly ``team_ids``.

        Manual memberships are untouched (and satisfy a mapped team as-is)."""
        want = set(team_ids)
        with self._lock:
            rows = self._query("SELECT team_id, source FROM team_members WHERE user_id = ?", (user_id,))
            have = {r["team_id"]: r["source"] for r in rows}
            for tid, src in have.items():
                if src == source and tid not in want:
                    self._execute(
                        "DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (tid, user_id)
                    )
            for tid in want:
                if tid not in have:
                    self._execute(
                        "INSERT INTO team_members (team_id, user_id, source, added_at) VALUES (?, ?, ?, ?)",
                        (tid, user_id, source, now),
                    )
        self._authz_invalidate()

    def delete_user_memberships(self, user_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
            self._execute(
                "DELETE FROM grants WHERE subject_type = ? AND subject_id = ?", (SUBJECT_USER, user_id)
            )
        self._authz_invalidate()

    # -- grants --------------------------------------------------------------

    def create_grant(self, g: GrantRow) -> None:
        self._execute(
            "INSERT INTO grants (id, subject_type, subject_id, scope_type, host_id, target, level, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g.id, g.subject_type, g.subject_id, g.scope_type, g.host_id, g.target, g.level,
             g.created_at, g.created_by),
        )
        self._authz_invalidate()

    def get_grant(self, grant_id: str) -> GrantRow:
        row = self._one("SELECT * FROM grants WHERE id = ?", (grant_id,))
        if row is None:
            raise NotFoundError("get grant: no rows in result set")
        return _row_to_grant(row)

    def list_grants(self, subject_type: str = "", subject_id: str = "") -> List[GrantRow]:
        if subject_type:
            rows = self._query(
                "SELECT * FROM grants WHERE subject_type = ? AND subject_id = ? ORDER BY created_at",
                (subject_type, subject_id),
            )
        else:
            rows = self._query("SELECT * FROM grants ORDER BY created_at")
        return [_row_to_grant(r) for r in rows]

    def delete_grant(self, grant_id: str) -> None:
        self._execute("DELETE FROM grants WHERE id = ?", (grant_id,))
        self._authz_invalidate()

    def delete_subject_grants(self, subject_type: str, subject_id: str) -> None:
        self._execute(
            "DELETE FROM grants WHERE subject_type = ? AND subject_id = ?", (subject_type, subject_id)
        )
        self._authz_invalidate()

    # -- snapshot ------------------------------------------------------------

    def authz_snapshot(self) -> AuthzSnapshot:
        snap = self._authz_cache
        if snap is not None and snap.generation == self._authz_gen:
            return snap
        with self._lock:
            gen = self._authz_gen
            mem_rows = self._query("SELECT team_id, user_id FROM team_members")
            grant_rows = self._query("SELECT * FROM grants")
        memberships: Dict[str, List[str]] = {}
        for r in mem_rows:
            memberships.setdefault(r["user_id"], []).append(r["team_id"])
        grants: Dict[Tuple[str, str], List[GrantRow]] = {}
        for r in grant_rows:
            g = _row_to_grant(r)
            grants.setdefault((g.subject_type, g.subject_id), []).append(g)
        snap = AuthzSnapshot(
            generation=gen,
            memberships={k: tuple(v) for k, v in memberships.items()},
            grants={k: tuple(v) for k, v in grants.items()},
        )
        self._authz_cache = snap
        return snap
