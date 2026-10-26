"""Audit log ``path`` filter: one resource's activity (the deployment page).

The filter keeps the exact path and every path below it, and treats LIKE
wildcards in the path literally so ``_`` or ``%`` never widen the match.
"""

import os
import tempfile
import unittest

from kowan.models import AuditEntry
from kowan.store.audit import AuditListOptions
from kowan.store.store import open_store


class TestAuditPathFilter(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = open_store(os.path.join(self._dir.name, "kowan.db"))
        paths = [
            "/api/v1/deployments/d1",
            "/api/v1/deployments/d1/deploy",
            "/api/v1/deployments/d1/protection",
            "/api/v1/deployments/d10/deploy",  # same prefix, different id
            "/api/v1/deployments/dX",  # would match d_ if '_' were a wildcard
            "/api/v1/hosts/h1",
        ]
        for i, p in enumerate(paths):
            self.store.create_audit(
                AuditEntry(id=f"e{i}", time=f"2026-10-25T10:00:0{i}Z", action="POST", path=p, status=200)
            )

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def _paths(self, path: str) -> list[str]:
        items, total = self.store.list_audit(AuditListOptions(limit=50, path=path))
        self.assertEqual(total, len(items))
        return sorted(e.path for e in items)

    def test_exact_and_children(self):
        self.assertEqual(
            self._paths("/api/v1/deployments/d1"),
            [
                "/api/v1/deployments/d1",
                "/api/v1/deployments/d1/deploy",
                "/api/v1/deployments/d1/protection",
            ],
        )

    def test_trailing_slash_is_ignored(self):
        self.assertEqual(len(self._paths("/api/v1/deployments/d1/")), 3)

    def test_wildcards_are_literal(self):
        self.assertEqual(self._paths("/api/v1/deployments/d_"), [])
        self.assertEqual(self._paths("/api/v1/deployments/%"), [])

    def test_empty_path_keeps_everything(self):
        self.assertEqual(len(self._paths("")), 6)


if __name__ == "__main__":
    unittest.main()
