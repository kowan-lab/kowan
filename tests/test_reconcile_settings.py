"""KOWAN_RECONCILE_INTERVAL / KOWAN_RECONCILE_COOLDOWN read like every other
duration setting: "15s", "5m", or plain seconds. Read at import, so each case
imports the deployment service in a fresh interpreter."""

import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PRINT = "import kowan.deployment.service as s; print(s._RECONCILE_INTERVAL, s._RECONCILE_COOLDOWN)"


def _read(**env):
    full = {k: v for k, v in os.environ.items() if not k.startswith("KOWAN_RECONCILE_")}
    full.update(env)
    out = subprocess.run([sys.executable, "-c", _PRINT], cwd=_ROOT, env=full, capture_output=True,
                         text=True, timeout=60, check=True)
    return tuple(float(v) for v in out.stdout.split())


class TestReconcileSettings(unittest.TestCase):
    def test_durations_and_plain_seconds(self):
        self.assertEqual(_read(), (5.0, 120.0))
        self.assertEqual(_read(KOWAN_RECONCILE_INTERVAL="15s", KOWAN_RECONCILE_COOLDOWN="5m"), (15.0, 300.0))
        self.assertEqual(_read(KOWAN_RECONCILE_INTERVAL="30", KOWAN_RECONCILE_COOLDOWN="1h30m"), (30.0, 5400.0))
        # Unparseable: the default, not a crash at startup.
        self.assertEqual(_read(KOWAN_RECONCILE_INTERVAL="soon"), (5.0, 120.0))


if __name__ == "__main__":
    unittest.main()
