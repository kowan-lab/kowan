"""Host CRUD.

Provides :class:`_HostMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from kowan.models import Host

from ._common import NotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_HOST_COLUMNS = "id, name, type, mode, address, token, last_seen, created_at"


def _row_to_host(row: sqlite3.Row) -> Host:
    # `online` is not persisted (the column was dropped): it is computed live via
    # a ping at response time (api/hosts.py) and defaults False on the model here.
    return Host(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        mode=row["mode"],
        address=row["address"],
        token=row["token"],
        lastSeen=row["last_seen"],
        createdAt=row["created_at"],
    )


class _HostMixin(_Base):
    def create_host(self, h: Host) -> None:
        """Insert a new host."""
        self._execute(
            "INSERT INTO hosts (id, name, type, mode, address, token, last_seen, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (h.id, h.name, h.type, h.mode, h.address, h.token, h.last_seen, h.created_at),
        )

    def set_host_last_seen(self, id: str, ts: str) -> None:
        """Record that a host was reachable at ``ts`` (RFC3339). The monitor calls
        this each tick a host probes online, so the ``last_seen`` column — and the
        UI's "last seen" — reflect the last real contact rather than enroll time."""
        self._execute("UPDATE hosts SET last_seen = ? WHERE id = ?", (ts, id))

    def rename_host(self, id: str, name: str) -> None:
        """Set a host's display name. The id, which everything else keys on,
        never changes."""
        self._execute("UPDATE hosts SET name = ? WHERE id = ?", (name, id))

    def get_host(self, id: str) -> Host:
        """Return the host with the given id, raising NotFoundError if absent."""
        row = self._one(f"SELECT {_HOST_COLUMNS} FROM hosts WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get host: no rows in result set")
        h = _row_to_host(row)
        h.tags = self.all_host_tags().get(id, [])
        return h

    def get_host_by_token(self, token: str) -> Host:
        """Return the host whose token matches (used to authenticate inbound edge
        tunnel connections by their enrollment token). Raises NotFoundError if
        no match."""
        row = self._one(f"SELECT {_HOST_COLUMNS} FROM hosts WHERE token = ?", (token,))
        if row is None:
            raise NotFoundError("get host: no rows in result set")
        return _row_to_host(row)

    def list_hosts(self) -> list[Host]:
        """Return all hosts ordered by creation time, with their tags."""
        rows = self._query(f"SELECT {_HOST_COLUMNS} FROM hosts ORDER BY created_at")
        tags = self.all_host_tags()
        out = [_row_to_host(r) for r in rows]
        for h in out:
            h.tags = tags.get(h.id, [])
        return out

    def delete_host(self, id: str) -> None:
        """Remove a host by id."""
        self._execute("DELETE FROM hosts WHERE id = ?", (id,))
        self._execute("DELETE FROM host_tags WHERE host_id = ?", (id,))

    # -- tags (host groups) ---------------------------------------------------
    def all_host_tags(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self._query("SELECT host_id, tag FROM host_tags ORDER BY tag"):
            out.setdefault(r["host_id"], []).append(r["tag"])
        return out

    def set_host_tags(self, id: str, tags: list[str]) -> None:
        """Replace a host's tags."""
        self._execute("DELETE FROM host_tags WHERE host_id = ?", (id,))
        for t in sorted(set(tags)):
            self._execute("INSERT INTO host_tags (host_id, tag) VALUES (?, ?)", (id, t))

    def hosts_with_tag(self, tag: str) -> list[str]:
        """Ids of the hosts in a group, oldest first."""
        rows = self._query("SELECT t.host_id FROM host_tags t JOIN hosts h ON h.id = t.host_id "
                           "WHERE t.tag = ? ORDER BY h.created_at", (tag,))
        return [r["host_id"] for r in rows]
