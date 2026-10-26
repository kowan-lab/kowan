import asyncio
import unittest

from kowan.transport.memory import memory_pair
from kowan.tunnel.endpoint import establish_server_side, establish_agent_side
from kowan.tunnel.hub import Hub

HOST_TOKEN = "tok-abc123"
HOST_ID = "host-1"


def server_verifier(token):
    # The control-plane resolves a token to a host record.
    return HOST_ID if token == HOST_TOKEN else None


async def ops_handler(stream):
    """Stand-in for the agent's Docker-ops handler: echo a 'pong'."""
    req = await stream.readall()
    await stream.write(b"pong:" + req)
    await stream.close()


def _spawn_ops(session):
    """Background accept-loop running ops_handler per control-plane stream.

    Mirrors agent_ops_server.serve_agent_ops (an explicit accept_stream loop);
    the mux no longer auto-serves via on_stream. Cancel the returned task in
    teardown.
    """
    async def loop():
        while True:
            s = await session.accept_stream()
            if s is None:
                return
            asyncio.ensure_future(ops_handler(s))
    return asyncio.ensure_future(loop())


class TestTunnel(unittest.IsolatedAsyncioTestCase):
    async def test_atos_edge_server_drives_agent(self):
        # agent dials the server (edge/NAT): agent SENDS token, server VERIFIES.
        t_agent, t_server = memory_pair()
        server_task = asyncio.ensure_future(
            establish_server_side(t_server, dialed=False, verify=server_verifier)
        )
        agent_sess = await establish_agent_side(
            t_agent, dialed=True, token=HOST_TOKEN
        )
        ops_task = _spawn_ops(agent_sess)
        host_id, server_sess = await server_task
        self.assertEqual(host_id, HOST_ID)

        st = await server_sess.open_stream()
        await st.write(b"ping")
        await st.close()
        self.assertEqual(await st.readall(), b"pong:ping")
        ops_task.cancel()
        await server_sess.close()
        await agent_sess.close()

    async def test_stoa_direct_agent_verifies_server_token(self):
        # server dials the agent (direct): server SENDS token, agent VERIFIES
        # it against its own configured token (the extra check).
        t_server, t_agent = memory_pair()
        agent_task = asyncio.ensure_future(
            establish_agent_side(t_agent, dialed=False, token=HOST_TOKEN)
        )
        host_id, server_sess = await establish_server_side(
            t_server, dialed=True, token=HOST_TOKEN, host_id=HOST_ID
        )
        agent_sess = await agent_task
        ops_task = _spawn_ops(agent_sess)
        self.assertEqual(host_id, HOST_ID)

        st = await server_sess.open_stream()
        await st.write(b"ping")
        await st.close()
        self.assertEqual(await st.readall(), b"pong:ping")
        ops_task.cancel()
        await server_sess.close()
        await agent_sess.close()

    async def test_bad_token_is_rejected_both_modes(self):
        # atos with a wrong token from the agent
        t_agent, t_server = memory_pair()
        server_task = asyncio.ensure_future(
            establish_server_side(t_server, dialed=False, verify=server_verifier)
        )
        with self.assertRaises(Exception):
            await establish_agent_side(t_agent, dialed=True, token="WRONG")
        with self.assertRaises(Exception):
            await server_task

        # stoa with a wrong token from the server
        t_server2, t_agent2 = memory_pair()
        agent_task = asyncio.ensure_future(
            establish_agent_side(t_agent2, dialed=False, token=HOST_TOKEN)
        )
        with self.assertRaises(Exception):
            await establish_server_side(t_server2, dialed=True, token="WRONG", host_id=HOST_ID)
        with self.assertRaises(Exception):
            await agent_task

    async def test_hub_dial_drives_agent(self):
        # atos + hub: server dials the agent through the hub and drives an op.
        hub = Hub()

        t_agent, t_server = memory_pair()
        server_task = asyncio.ensure_future(
            establish_server_side(t_server, dialed=False, verify=server_verifier)
        )
        agent_sess = await establish_agent_side(
            t_agent, dialed=True, token=HOST_TOKEN
        )
        ops_task = _spawn_ops(agent_sess)
        host_id, server_sess = await server_task
        await hub.register(host_id, server_sess)
        self.assertTrue(hub.connected(HOST_ID))

        # control-plane -> agent, via the hub
        st = await hub.dial(HOST_ID)
        await st.write(b"list")
        await st.close()
        self.assertEqual(await st.readall(), b"pong:list")

        ops_task.cancel()
        await server_sess.close()
        await agent_sess.close()

    async def test_hub_dial_unknown_host(self):
        hub = Hub()
        with self.assertRaises(KeyError):
            await hub.dial("nope")


if __name__ == "__main__":
    unittest.main()
