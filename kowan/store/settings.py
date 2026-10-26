"""Key/value settings CRUD.

Provides :class:`_SettingsMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


class _SettingsMixin(_Base):
    def get_setting(self, key: str) -> str:
        """Return the value for key, or "" if the key is not present.

        A missing row is not an error.
        """
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return ""
        return row["value"]

    def set_setting(self, key: str, value: str) -> None:
        """Insert or update the value for key (upsert on the primary key)."""
        self._execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
