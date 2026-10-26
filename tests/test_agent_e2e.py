"""The real agent against the real server, both transports, Docker = fake Engine:

    edge:   kowan.agent._run_edge  --dials-->  server /edge/connect
    direct: server StoaDialer      --dials-->  kowan.agent._run_agent (wss, self-signed)

then the control plane lists the agent host's containers through the REST API.
Plus the agent's entrypoint and small helpers."""

import asyncio
import io
import os
import re
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kowan import agent, server
from kowan.config import AgentConfig
from kowan.docker.local import LocalClient
from tests.apicase import ServerCase


def _token(created) -> str:
    """The host token, as the join command carries it (the host JSON never does)."""
    return re.search(r"KOWAN_AGENT_TOKEN=(\S+)", created["dockerRun"]).group(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AgentCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._hb = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"KOWAN_AGENT_HEARTBEAT_FILE": os.path.join(self._hb, "hb")})
        self._env.start()
        self.engine.add_container("remote-web", "nginx:1.27")
        self.ops = LocalClient(self.engine.socket_path)
        self.stop = asyncio.Event()
        self.task = None

    async def asyncTearDown(self):
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 10)
        await self.ops.close()
        self._env.stop()
        await super().asyncTearDown()

    async def wait_online(self, host_id):
        for _ in range(200):
            _, h = await self.call("GET", f"/hosts/{host_id}", "viewer")
            if h.get("online"):
                return h
            await asyncio.sleep(0.05)
        self.fail("host never came online")


class TestEdgeAgent(AgentCase):
    async def test_edge_agent_serves_the_control_plane(self):
        _, created = await self.call("POST", "/hosts", "admin", json={"name": "edge1", "mode": "edge"}, expect=201)
        host = created["host"]
        self.assertIn("KOWAN_AGENT_MODE=edge", created["dockerRun"])
        cfg = AgentConfig(mode="edge", server_url=str(self.client.make_url("")).rstrip("/"),
                          edge_token=_token(created))
        self.assertEqual(agent.config_problem(cfg), "")
        self.task = asyncio.ensure_future(agent._run_edge(cfg, self.ops, self.stop))
        await self.wait_online(host["id"])
        _, items = await self.call("GET", f"/hosts/{host['id']}/containers", "viewer", params={"all": "1"}, expect=200)
        self.assertEqual([c["names"] for c in items], [["/remote-web"]])
        self.assertTrue(os.path.exists(os.environ["KOWAN_AGENT_HEARTBEAT_FILE"]))

    async def test_wrong_token_keeps_retrying_until_stopped(self):
        cfg = AgentConfig(mode="edge", server_url=str(self.client.make_url("")).rstrip("/"), edge_token="x" * 64)
        with mock.patch.object(agent, "_MIN_BACKOFF", 0.05), mock.patch.object(agent, "_MAX_BACKOFF", 0.1):
            self.task = asyncio.ensure_future(agent._run_edge(cfg, self.ops, self.stop))
            await asyncio.sleep(0.4)
            self.assertFalse(self.task.done())
            self.stop.set()
            await asyncio.wait_for(self.task, 5)

    async def test_edge_needs_a_server_url(self):
        with self.assertRaises(ValueError):
            await agent._run_edge(AgentConfig(mode="edge", edge_token="t"), self.ops, self.stop)


class TestDirectAgent(AgentCase):
    async def test_server_dials_the_agent(self):
        port = _free_port()
        _, created = await self.call("POST", "/hosts", "admin", json={
            "name": "direct1", "mode": "agent", "address": f"127.0.0.1:{port}"}, expect=201)
        host = created["host"]
        self.assertIn("-p 9001:9001", created["dockerRun"])
        cfg = AgentConfig(mode="agent", addr=f"127.0.0.1:{port}", token=_token(created), allow_cidrs="127.0.0.1/32")
        self.assertEqual(agent.config_problem(cfg), "")
        self.task = asyncio.ensure_future(agent._run_agent(cfg, self.ops, self.stop))
        for _ in range(100):  # wait for the listener
            try:
                _, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                break
            except OSError:
                await asyncio.sleep(0.05)
        self.client.app[server.DIALER_KEY]._reconcile()
        await self.wait_online(host["id"])
        _, items = await self.call("GET", f"/hosts/{host['id']}/containers", "viewer", params={"all": "1"}, expect=200)
        self.assertEqual([c["names"] for c in items], [["/remote-web"]])
        await self.call("DELETE", f"/hosts/{host['id']}", "admin", expect=200)


class TestEntrypointAndHelpers(unittest.TestCase):
    def test_config_problem(self):
        self.assertIn("KOWAN_AGENT_TOKEN", agent.config_problem(AgentConfig(mode="edge")))
        self.assertIn("at least", agent.config_problem(AgentConfig(token="short")))
        self.assertIn("KOWAN_AGENT_ALLOW_CIDRS", agent.config_problem(AgentConfig(token="t" * 40, allow_cidrs="nope")))
        self.assertEqual(agent.config_problem(AgentConfig(token="t" * 40)), "")

    def test_helpers(self):
        self.assertEqual(agent._token_fingerprint(""), "(none)")
        self.assertEqual(len(agent._token_fingerprint("abc")), 12)
        self.assertEqual(agent._edge_connect_url("https://k.example/"), "wss://k.example/edge/connect")
        self.assertEqual(agent._edge_connect_url("http://k:5081"), "ws://k:5081/edge/connect")
        self.assertEqual(agent._edge_connect_url("wss://k"), "wss://k/edge/connect")
        self.assertEqual(agent._edge_connect_url("k:5081"), "ws://k:5081/edge/connect")
        self.assertEqual(agent._split_addr(":9001"), (None, 9001))
        self.assertEqual(agent._split_addr("0.0.0.0:9002"), ("0.0.0.0", 9002))
        self.assertEqual(agent._split_addr("9003"), (None, 9003))
        self.assertEqual(agent._split_addr("myhost"), ("myhost", 9001))
        self.assertEqual(agent._split_addr("h:bad"), ("h", 9001))

    def test_provided_certificate(self):
        cert, key = agent._generate_self_signed_cert()
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "c.pem"), "wb") as f:
            f.write(cert)
        with open(os.path.join(d, "k.pem"), "wb") as f:
            f.write(key)
        _, der = agent._build_ssl_context(AgentConfig(tls_cert=os.path.join(d, "c.pem"), tls_key=os.path.join(d, "k.pem")))
        self.assertTrue(der)

    def test_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            agent.main(["--help"])
            agent.main(["--version"])
        self.assertIn("kowan-agent", out.getvalue())
        with mock.patch.dict(os.environ, {"KOWAN_AGENT_MODE": "agent", "KOWAN_AGENT_TOKEN": "short"}):
            with self.assertRaises(SystemExit) as cm:
                agent.main([])
        self.assertEqual(cm.exception.code, 2)
        with mock.patch.dict(os.environ, {"KOWAN_AGENT_HEARTBEAT_FILE": "/nonexistent/hb"}):
            with self.assertRaises(SystemExit) as cm:
                agent.main(["healthcheck"])
        self.assertNotEqual(cm.exception.code, 0)
        with mock.patch.dict(os.environ, {"KOWAN_AGENT_MODE": "agent", "KOWAN_AGENT_TOKEN": "t" * 40}), \
                mock.patch.object(agent, "_run", mock.MagicMock(return_value=None)) as run, \
                mock.patch("kowan.eventloop.run") as loop_run:
            agent.main([])
        loop_run.assert_called_once()
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
