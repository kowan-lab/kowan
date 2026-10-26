"""The docker/git CLI helper is bounded, and image builds cannot escape their
temp folder or smuggle git options."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from kowan.docker import local
from kowan.docker.local import _contained, _run_command
from kowan.errors import Validation


class TestRunCommand(unittest.TestCase):
    def test_timeout_kills(self):
        out, code = asyncio.run(_run_command("sleep", "5", timeout=0.3))
        self.assertIn("killed after", out)
        self.assertNotEqual(code, 0)

    def test_output_is_capped(self):
        with mock.patch.object(local, "COMMAND_OUTPUT_LIMIT", 1000):
            out, code = asyncio.run(_run_command("head", "-c", "100000", "/dev/zero"))
        self.assertEqual(code, 0)
        self.assertIn("more bytes of output not kept", out)
        self.assertLess(len(out), 1200)

    def test_stdin_and_env(self):
        out, code = asyncio.run(_run_command("sh", "-c", "cat; echo $ONLY", stdin=b"in-", env={"ONLY": "x", "PATH": os.environ["PATH"]}))
        self.assertEqual((out.strip(), code), ("in-x", 0))


class TestBuildContext(unittest.TestCase):
    def test_context_stays_inside(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "app"))
        self.assertEqual(_contained(root, "app"), os.path.realpath(os.path.join(root, "app")))
        for bad in ("../", "/data", "app/../../etc"):
            with self.assertRaises(Validation, msg=bad):
                _contained(root, bad)

    def test_symlink_out_is_refused(self):
        root = tempfile.mkdtemp()
        os.symlink("/", os.path.join(root, "escape"))
        with self.assertRaises(Validation):
            _contained(root, "escape")


if __name__ == "__main__":
    unittest.main()
