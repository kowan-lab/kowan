"""End-to-end test of the docker-ops-over-tunnel path:

    RemoteClient (control-plane)  --mux stream-->  agent serve_ops  -->  Ops

proving the agent protocol + remote client + mux wire together, with a fake
Ops on the agent side (no Docker daemon needed).
"""

import asyncio
import unittest

from kowan.mux.session import Session
from kowan.transport.memory import memory_pair
from kowan.agent_ops_server import serve_agent_ops
from kowan.docker.remote import make_remote_client
from kowan.models import Container


class FakeOps:
    """A stand-in agent Ops that returns canned data (duck-typed; serve_ops
    dispatches by method name)."""

    def __init__(self):
        self.pinged = False

    async def ping(self):
        self.pinged = True
        return None

    async def list_containers(self, all):
        self.list_all = all
        return [
            Container(id="c1", image="nginx", state="running"),
            Container(id="c2", image="redis", state="exited"),
        ]


class TestTunnelOps(unittest.IsolatedAsyncioTestCase):
    async def test_remote_ops_over_tunnel(self):
        ta, tb = memory_pair()
        control_plane = Session(ta, is_client=True).start()
        agent = Session(tb, is_client=False).start()
        ops = FakeOps()
        serve_task = asyncio.ensure_future(serve_agent_ops(agent, ops))

        # The control-plane drives the host through a RemoteClient whose dial
        # opens a fresh mux stream (this is exactly what Hub.dial provides).
        remote = make_remote_client(lambda: control_plane.open_stream())

        await remote.ping()
        self.assertTrue(ops.pinged, "ping did not reach the agent over the tunnel")

        containers = await remote.list_containers(all=True)
        self.assertEqual([c.id for c in containers], ["c1", "c2"])
        self.assertEqual(containers[0].image, "nginx")
        self.assertEqual(containers[1].state, "exited")
        self.assertTrue(ops.list_all, "op args did not round-trip")

        serve_task.cancel()
        await control_plane.close()
        await agent.close()


if __name__ == "__main__":
    unittest.main()
