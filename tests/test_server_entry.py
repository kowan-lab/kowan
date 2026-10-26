"""The server entrypoint without opening a port: address parsing, the TLS
context, the healthcheck sub-command, a staged restore applied at startup,
and main() wiring build_app into web.run_app with the redacting access log."""

import os
import tempfile
import unittest
from unittest import mock

from kowan import server
from kowan.agent import _generate_self_signed_cert
from tests.apicase import config


class TestEntrypoint(unittest.TestCase):
    def test_split_addr(self):
        self.assertEqual(server._split_addr(""), (None, 5081))
        self.assertEqual(server._split_addr(":8080"), (None, 8080))
        self.assertEqual(server._split_addr("127.0.0.1:9000"), ("127.0.0.1", 9000))
        self.assertEqual(server._split_addr("7000"), (None, 7000))
        self.assertEqual(server._split_addr("myhost"), ("myhost", 5081))
        self.assertEqual(server._split_addr("h:nope"), ("h", 5081))

    def test_tls_context(self):
        cfg = config(tempfile.mkdtemp(), "/nonexistent.sock")
        self.assertIsNone(server._tls_context(cfg))
        cert, key = _generate_self_signed_cert()
        d = tempfile.mkdtemp()
        for name, data in (("c.pem", cert), ("k.pem", key)):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        cfg = config(d, "/nonexistent.sock", tls_cert=os.path.join(d, "c.pem"), tls_key=os.path.join(d, "k.pem"))
        self.assertIsNotNone(server._tls_context(cfg))

    def test_main_and_subcommands(self):
        with mock.patch("sys.argv", ["kowan", "healthcheck"]), \
                mock.patch.dict(os.environ, {"KOWAN_ADDR": "127.0.0.1:1"}):
            with self.assertRaises(SystemExit) as cm:
                server.main()
            self.assertNotEqual(cm.exception.code, 0)
        data_dir = tempfile.mkdtemp()
        env = {"KOWAN_DATA_DIR": data_dir, "KOWAN_ADDR": "127.0.0.1:0", "KOWAN_DOCKER_SOCKET": "/nonexistent.sock"}
        with mock.patch("sys.argv", ["kowan"]), mock.patch.dict(os.environ, env), \
                mock.patch.object(server.web, "run_app") as run_app:
            server.main()
        run_app.assert_called_once()
        kwargs = run_app.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["port"]), ("127.0.0.1", 0))
        self.assertIsNone(kwargs["ssl_context"])


if __name__ == "__main__":
    unittest.main()
