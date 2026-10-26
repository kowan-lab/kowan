"""Unit tests for swarm node listing (Engine /nodes -> SwarmNode mapping).

Exercises the pure mapping helper and the LocalClient.list_swarm_nodes control
flow WITHOUT a live Docker socket. The mapping is factored into
``kowan.docker.local._swarm_nodes_from_engine`` so it is testable against a
canned Engine payload; the "not a swarm manager" 503 path is driven by
monkeypatching ``_request_json`` on a LocalClient instance (its aiohttp session
is created lazily, so no socket is opened).
"""

import asyncio
import unittest

from kowan.docker.local import LocalClient, _swarm_nodes_from_engine
from kowan.errors import BadGateway

# A representative Docker /nodes response: one manager+leader, one worker, one
# down manager missing Engine and Status.Addr (address falls back to
# ManagerStatus.Addr).
NODES_PAYLOAD = [
    {  # manager + leader, fully populated
        "ID": "mgr1",
        "Spec": {"Role": "manager", "Availability": "active"},
        "Description": {
            "Hostname": "node-a",
            "Engine": {"EngineVersion": "24.0.7"},
        },
        "Status": {"State": "ready", "Addr": "10.0.0.1"},
        "ManagerStatus": {
            "Leader": True,
            "Reachability": "reachable",
            "Addr": "10.0.0.1:2377",
        },
    },
    {  # worker: no ManagerStatus block at all
        "ID": "wkr1",
        "Spec": {"Role": "worker", "Availability": "active"},
        "Description": {
            "Hostname": "node-b",
            "Engine": {"EngineVersion": "24.0.7"},
        },
        "Status": {"State": "ready", "Addr": "10.0.0.2"},
    },
    {  # down manager: no Engine, no Status.Addr -> address from ManagerStatus.Addr
        "ID": "mgr2",
        "Spec": {"Role": "manager", "Availability": "drain"},
        "Description": {"Hostname": "node-c"},
        "Status": {"State": "down"},
        "ManagerStatus": {"Leader": False, "Addr": "10.0.0.3:2377"},
    },
]


class TestSwarmNodeMapping(unittest.TestCase):
    def test_maps_all_fields(self):
        nodes = _swarm_nodes_from_engine(NODES_PAYLOAD)
        self.assertEqual(len(nodes), 3)

        mgr, wkr, down = nodes

        # manager + leader
        self.assertEqual(mgr.id, "mgr1")
        self.assertEqual(mgr.hostname, "node-a")
        self.assertEqual(mgr.role, "manager")
        self.assertTrue(mgr.leader)
        self.assertEqual(mgr.availability, "active")
        self.assertEqual(mgr.status, "ready")
        self.assertEqual(mgr.address, "10.0.0.1")  # prefers Status.Addr
        self.assertEqual(mgr.engine_version, "24.0.7")

        # worker: no ManagerStatus -> leader False, address from Status.Addr
        self.assertEqual(wkr.id, "wkr1")
        self.assertEqual(wkr.hostname, "node-b")
        self.assertEqual(wkr.role, "worker")
        self.assertFalse(wkr.leader)
        self.assertEqual(wkr.availability, "active")
        self.assertEqual(wkr.status, "ready")
        self.assertEqual(wkr.address, "10.0.0.2")
        self.assertEqual(wkr.engine_version, "24.0.7")

        # down manager: no Engine, address falls back to ManagerStatus.Addr
        self.assertEqual(down.id, "mgr2")
        self.assertEqual(down.hostname, "node-c")
        self.assertEqual(down.role, "manager")
        self.assertFalse(down.leader)
        self.assertEqual(down.availability, "drain")
        self.assertEqual(down.status, "down")
        self.assertEqual(down.address, "10.0.0.3:2377")  # ManagerStatus.Addr fallback
        self.assertEqual(down.engine_version, "")  # no Engine block

    def test_defensive_against_missing_blocks(self):
        # A node object with nothing but an ID must not raise and defaults empties.
        nodes = _swarm_nodes_from_engine([{"ID": "bare"}, None])
        self.assertEqual(len(nodes), 1)  # None entries are skipped
        n = nodes[0]
        self.assertEqual(n.id, "bare")
        self.assertEqual(n.hostname, "")
        self.assertEqual(n.role, "")
        self.assertFalse(n.leader)
        self.assertEqual(n.availability, "")
        self.assertEqual(n.status, "")
        self.assertEqual(n.address, "")
        self.assertEqual(n.engine_version, "")

    def test_empty_input(self):
        self.assertEqual(_swarm_nodes_from_engine(None), [])
        self.assertEqual(_swarm_nodes_from_engine([]), [])

    def test_camelcase_json_aliases(self):
        node = _swarm_nodes_from_engine(NODES_PAYLOAD)[0]
        js = node.model_dump(by_alias=True)
        # engineVersion is the only aliased field; the rest are already camel/flat.
        self.assertEqual(js["engineVersion"], "24.0.7")
        self.assertEqual(js["leader"], True)
        self.assertEqual(js["role"], "manager")


class TestLocalClientListSwarmNodes(unittest.TestCase):
    def _client(self, request_json):
        client = LocalClient(socket_path="/nonexistent.sock")
        client._request_json = request_json  # type: ignore[assignment]
        return client

    def test_happy_path_hits_nodes_and_maps(self):
        seen = {}

        async def fake(method, path, **kw):
            seen["method"] = method
            seen["path"] = path
            return NODES_PAYLOAD

        client = self._client(fake)
        nodes = asyncio.run(client.list_swarm_nodes())
        self.assertEqual(seen, {"method": "GET", "path": "/nodes"})
        self.assertEqual([n.id for n in nodes], ["mgr1", "wkr1", "mgr2"])

    def test_not_a_manager_503_returns_empty(self):
        async def boom(method, path, **kw):
            # Shape produced by LocalClient._engine_error for a 503 on /nodes.
            raise BadGateway("docker: GET /nodes: 503 This node is not a swarm manager")

        client = self._client(boom)
        self.assertEqual(asyncio.run(client.list_swarm_nodes()), [])

    def test_other_bad_gateway_propagates(self):
        async def boom(method, path, **kw):
            raise BadGateway("docker: GET /nodes: 500 internal server error")

        client = self._client(boom)
        with self.assertRaises(BadGateway):
            asyncio.run(client.list_swarm_nodes())


if __name__ == "__main__":
    unittest.main()
