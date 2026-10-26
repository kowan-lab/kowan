"""User CRUD.

Provides :class:`_UserMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from kowan.models import User

from ._common import NotFoundError, StoreError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


# AuthSource value for built-in username/password users.
# Kept local to the store so schema/CRUD do not depend on the models module's
# constant layout.
AUTH_SOURCE_LOCAL = "local"

# Canonical column list / scan order for a user row.
_USER_COLUMNS = "id, username, role, password_hash, email, auth_source, created_at"


def _row_to_user(row: sqlite3.Row) -> User:
    """Build a User model from a users row (column order == _USER_COLUMNS)."""
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        passwordHash=row["password_hash"],
        email=row["email"],
        authSource=row["auth_source"],
        createdAt=row["created_at"],
    )


class _UserMixin(_Base):
    def create_user(self, u: User) -> None:
        """Insert a new user. A blank auth source defaults to 'local'."""
        auth_source = u.auth_source or AUTH_SOURCE_LOCAL
        self._execute(
            "INSERT INTO users (id, username, role, password_hash, email, auth_source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (u.id, u.username, u.role, u.password_hash, u.email, auth_source, u.created_at),
        )

    def get_user(self, id: str) -> User:
        """Return the user with the given id, raising NotFoundError if absent."""
        row = self._one(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get user: no rows in result set")
        return _row_to_user(row)

    def get_user_by_username(self, name: str) -> User:
        """Return the user with the given username, raising NotFoundError if absent."""
        row = self._one(f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (name,))
        if row is None:
            raise NotFoundError("get user: no rows in result set")
        return _row_to_user(row)

    def get_user_by_email(self, email: str) -> User:
        """Return the user with the given email, matched case-insensitively.

        Blank emails never match (local accounts without an email are not
        selectable by an external identity).
        """
        if email == "":
            raise StoreError("get user by email: empty email")
        row = self._one(
            f"SELECT {_USER_COLUMNS} FROM users WHERE email <> '' AND lower(email) = lower(?)",
            (email,),
        )
        if row is None:
            raise NotFoundError("get user: no rows in result set")
        return _row_to_user(row)

    def list_users(self) -> list[User]:
        """Return all users ordered by creation time."""
        rows = self._query(f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at")
        return [_row_to_user(r) for r in rows]

    def update_user(self, u: User) -> None:
        """Update the mutable fields of a user."""
        self._execute(
            "UPDATE users SET username = ?, role = ?, password_hash = ?, email = ?, auth_source = ? WHERE id = ?",
            (u.username, u.role, u.password_hash, u.email, u.auth_source, u.id),
        )

    def delete_user(self, id: str) -> None:
        """Remove a user by id."""
        self._execute("DELETE FROM users WHERE id = ?", (id,))

    def count_users(self) -> int:
        """Return the total number of users."""
        row = self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row is not None else 0
