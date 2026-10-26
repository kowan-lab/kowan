"""Network policy: proxy headers count only from trusted proxies, and the UI and
edge-agent endpoints can each be limited to a set of addresses."""

import datetime
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kowan.agent import config_problem
from kowan.config import AgentConfig, ServerConfig
from kowan.server import build_app
from kowan.web.netpolicy import NetPolicy, parse_cidrs


def _req(path="/", remote="203.0.113.7", headers=None):
    req = make_mocked_request("GET", path, headers=headers or {})
    return mock.Mock(wraps=req, remote=remote, headers=req.headers, path=path)


class TestClientAddress(unittest.TestCase):
    def test_headers_ignored_from_untrusted_peers(self):
        p = NetPolicy(trusted_proxies=parse_cidrs("10.0.0.1"))
        r = _req(remote="203.0.113.7", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
        self.assertEqual(str(p.client_address(r)), "203.0.113.7")

    def test_forwarded_for_from_trusted_proxy(self):
        p = NetPolicy(trusted_proxies=parse_cidrs("10.0.0.0/24"))
        # A client can prepend anything; the last untrusted hop is the truth.
        r = _req(remote="10.0.0.1", headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.4, 10.0.0.2"})
        self.assertEqual(str(p.client_address(r)), "198.51.100.4")

    def test_ports_and_brackets_in_forwarded_for(self):
        p = NetPolicy(trusted_proxies=parse_cidrs("10.0.0.5"))
        r = _req(remote="10.0.0.5", headers={"X-Forwarded-For": "203.0.113.9:4444"})
        self.assertEqual(str(p.client_address(r)), "203.0.113.9")
        r = _req(remote="10.0.0.5", headers={"X-Forwarded-For": "[2001:db8::1]:443"})
        self.assertEqual(str(p.client_address(r)), "2001:db8::1")

    def test_unreadable_hop_is_unknown_not_the_proxy(self):
        # Falling back to the proxy's own address would let anyone through an
        # allowlist that contains the proxy's network.
        p = NetPolicy(trusted_proxies=parse_cidrs("10.0.0.5"), ui_allow=parse_cidrs("10.0.0.0/8"))
        r = _req("/api/v1/me", remote="10.0.0.5", headers={"X-Forwarded-For": "unknown"})
        self.assertIsNone(p.client_address(r))
        self.assertFalse(p.allowed(r))

    def test_only_the_configured_header_counts(self):
        p = NetPolicy(trusted_proxies=parse_cidrs("10.0.0.5"))
        r = _req(remote="10.0.0.5", headers={"X-Forwarded-For": "198.51.100.4", "X-Real-IP": "127.0.0.1"})
        self.assertEqual(str(p.client_address(r)), "198.51.100.4")
        p.proxy_header = "x-real-ip"
        self.assertEqual(str(p.client_address(r)), "127.0.0.1")

    def test_health_is_an_exact_path(self):
        p = NetPolicy(ui_allow=parse_cidrs("192.168.1.0/24"))
        self.assertFalse(p.allowed(_req("/healthz-admin", remote="203.0.113.7")))

    def test_ipv4_mapped_peer(self):
        p = NetPolicy(ui_allow=parse_cidrs("192.168.1.0/24"))
        self.assertTrue(p.allowed(_req("/api/v1/me", remote="::ffff:192.168.1.20")))

    def test_bad_cidr_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_cidrs("10.0.0.0/33")


class TestAllowLists(unittest.TestCase):
    def setUp(self):
        self.p = NetPolicy(ui_allow=parse_cidrs("192.168.1.0/24"), agent_allow=parse_cidrs("198.51.100.0/24"))

    def test_ui_and_agent_lists_apply_to_their_paths(self):
        self.assertTrue(self.p.allowed(_req("/api/v1/me", remote="192.168.1.5")))
        self.assertFalse(self.p.allowed(_req("/api/v1/me", remote="198.51.100.5")))
        self.assertFalse(self.p.allowed(_req("/", remote="203.0.113.7")))
        self.assertTrue(self.p.allowed(_req("/edge/connect", remote="198.51.100.5")))
        self.assertFalse(self.p.allowed(_req("/edge/connect", remote="192.168.1.5")))

    def test_webhooks_and_health_stay_reachable(self):
        self.assertTrue(self.p.allowed(_req("/hooks/deploy/x", remote="203.0.113.7")))
        self.assertTrue(self.p.allowed(_req("/health", remote="203.0.113.7")))

    def test_empty_lists_do_not_restrict(self):
        self.assertTrue(NetPolicy().allowed(_req("/api/v1/me", remote="203.0.113.7")))


class TestServerWiring(unittest.IsolatedAsyncioTestCase):
    async def test_ui_list_blocks_other_addresses(self):
        cfg = ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                           monitor_interval=datetime.timedelta(seconds=3600),
                           metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="",
                           ui_allow_cidrs="192.0.2.0/24")
        app, _ = await build_app(cfg)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            self.assertEqual((await client.get("/api/v1/auth/status")).status, 403)
            self.assertEqual((await client.get("/health")).status, 200)
        finally:
            await client.close()

    async def test_bad_cidr_refuses_to_start(self):
        cfg = ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                           monitor_interval=datetime.timedelta(seconds=3600),
                           metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="",
                           trusted_proxies="not-an-ip")
        with self.assertRaises(ValueError):
            await build_app(cfg)


class TestAgentAllowList(unittest.TestCase):
    def test_bad_agent_cidr_is_a_config_problem(self):
        self.assertTrue(config_problem(AgentConfig(mode="agent", token="a" * 64, allow_cidrs="nope")))
        self.assertEqual(config_problem(AgentConfig(mode="agent", token="a" * 64, allow_cidrs="10.0.0.0/8")), "")


if __name__ == "__main__":
    unittest.main()
