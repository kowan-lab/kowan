"""Package re-exports resolve on first use, and only load their submodule."""

import subprocess
import sys
import unittest

import kowan.docker
import kowan.models
import kowan.tunnel
import kowan.web


class TestLazyExports(unittest.TestCase):
    def test_every_public_name_resolves(self):
        for pkg in (kowan.models, kowan.docker, kowan.tunnel, kowan.web):
            for name in pkg.__all__:
                self.assertIsNotNone(getattr(pkg, name), f"{pkg.__name__}.{name}")
            self.assertTrue(set(pkg.__all__) <= set(dir(pkg)))

    def test_unknown_name(self):
        with self.assertRaises(AttributeError):
            kowan.models.NoSuchModel  # noqa: B018
        with self.assertRaises(ImportError):
            from kowan.models import NoSuchModel  # noqa: F401

    def test_agent_skips_server_modules(self):
        # The agent needs neither auth nor the workflow models nor the hub.
        code = ("import sys, kowan.agent; print(' '.join(m for m in sys.modules if m.startswith('kowan')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
        for mod in ("kowan.auth", "kowan.models.workflow", "kowan.models.alert", "kowan.tunnel.hub", "kowan.web.app"):
            self.assertNotIn(mod, out)


if __name__ == "__main__":
    unittest.main()
