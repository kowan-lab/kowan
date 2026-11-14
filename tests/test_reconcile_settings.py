"""Reconcile interval and cooldown are settings, read at each use."""

import unittest

from kowan import tunables
from kowan.deployment import service as s


class TestReconcileSettings(unittest.TestCase):
    def test_defaults_and_live_change(self):
        with tunables.override():
            tunables._saved.pop("reconcile_interval", None)
            tunables._saved.pop("reconcile_cooldown", None)
            self.assertEqual((s._reconcile_interval(), s._reconcile_cooldown()), (5.0, 120.0))
        with tunables.override(reconcile_interval="15s", reconcile_cooldown="5m"):
            self.assertEqual((s._reconcile_interval(), s._reconcile_cooldown()), (15.0, 300.0))
        with tunables.override(reconcile_interval="30", reconcile_cooldown="1h30m"):
            self.assertEqual((s._reconcile_interval(), s._reconcile_cooldown()), (30.0, 5400.0))


if __name__ == "__main__":
    unittest.main()
