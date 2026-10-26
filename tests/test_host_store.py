"""Host store: last_seen refresh.

The ``online`` boolean is no longer persisted (liveness is probed live); the
``last_seen`` timestamp IS persisted and the monitor refreshes it each tick a
host is reachable via :meth:`set_host_last_seen`, so the UI's "last seen" is
truthful instead of frozen at enrollment time.
"""

import os
import tempfile
import unittest

from kowan.models import Host
from kowan.store import Store


class TestHostStore(unittest.TestCase):
    def _fresh_store(self) -> Store:
        d = tempfile.mkdtemp()
        return Store(os.path.join(d, "t.db"))

    def test_set_host_last_seen_updates_the_row(self):
        store = self._fresh_store()
        store.create_host(
            Host(
                id="h1", name="h1", type="agent", mode="agent", address="",
                token="t", lastSeen="enroll-time", createdAt="c",
            )
        )
        store.set_host_last_seen("h1", "2026-07-09T10:00:00Z")
        self.assertEqual(store.get_host("h1").last_seen, "2026-07-09T10:00:00Z")



if __name__ == "__main__":
    unittest.main()
