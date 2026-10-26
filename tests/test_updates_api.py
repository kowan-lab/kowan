"""HTTP contract of the self-update API (roles, status codes, shapes)."""

import datetime
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.api import hosts, serverupdate
from kowan.auth import new_jwt
from kowan.config import ServerConfig
from kowan.models import Host
from kowan.store import Store
from kowan.updater.orchestrator import UpdateError
from kowan.web import Services, create_app



def _ensure_user(store, role):
    """Tokens are checked against the users table: mint them for real users."""
    from kowan.models import User

    uid = "u-" + role
    try:
        store.get_user(uid)
    except Exception:  # noqa: BLE001
        store.create_user(User(id=uid, username=role, role=role, passwordHash="x",
                               createdAt="2026-01-01T00:00:00Z"))
    return uid

class StubManager:
    def __init__(self):
        self.calls = []
        self.fail = None

    def live(self, _id):
        return False

    async def status(self, component, host=None):
        self.calls.append(("status", component, getattr(host, "id", None)))
        return {"supported": True, "reason": "", "error": "", "image": "x:1", "container": "c",
                "version": "1", "running": "", "latest": "", "updateAvailable": False,
                "canRollback": False, "previous": None, "inProgress": None, "last": None}

    async def start(self, component, host=None, *, mode="update", image="", user="", restore_db=True):
        self.calls.append(("start", component, getattr(host, "id", None), mode, image, user, restore_db))
        if self.fail:
            raise self.fail
        return "id-" + mode

    async def start_batch(self, hosts, *, image="", user=""):
        self.calls.append(("batch", [h.id for h in hosts], image, user))
        return {"batchId": "b1", "ids": ["i1", "i2"][: len(hosts)]}


class TestUpdatesAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                           monitor_interval=datetime.timedelta(seconds=3600),
                           metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="")
        self.store = Store(":memory:")
        self.jwt = new_jwt("x" * 48)
        self.svc = Services(config=cfg, store=self.store, jwt=self.jwt)
        self.mgr = StubManager()
        self.svc.attach("updates", self.mgr)
        for hid, typ in (("local", "local"), ("h1", "agent"), ("h2", "agent")):
            self.store.create_host(Host(id=hid, name=hid, type=typ, mode="edge", address="", token=hid,
                                        lastSeen="", createdAt="2026-01-01T00:00:00Z"))
        self.store.save_update({"id": "s1", "component": "server", "status": "updated", "mode": "update",
                                "startedAt": "2026-10-25T10:00:00Z"})
        self.store.save_update({"id": "a1", "component": "agent", "hostId": "h1", "status": "rolled_back",
                                "mode": "update", "startedAt": "2026-10-25T10:00:00Z"})
        app = create_app(self.svc, registrars=[hosts.register, serverupdate.register])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def h(self, role):
        return {"Authorization": "Bearer " + self.jwt.issue(_ensure_user(self.store, role), role, role)}

    async def req(self, method, path, role="admin", **kw):
        r = await self.client.request(method, "/api/v1" + path, headers=self.h(role), **kw)
        try:
            body = await r.json()
        except Exception:  # noqa: BLE001
            body = await r.text()
        return r.status, body

    async def test_server_routes_admin_only(self):
        for method, path in (("GET", "/system/server/update"), ("POST", "/system/server/update"),
                             ("POST", "/system/server/rollback"), ("GET", "/system/server/updates")):
            st, _ = await self.req(method, path, role="operator")
            self.assertEqual(st, 403, path)
        st, body = await self.req("GET", "/system/server/update")
        self.assertEqual(st, 200)
        self.assertTrue(body["supported"])

    async def test_server_update_and_rollback(self):
        st, body = await self.req("POST", "/system/server/update", json={"image": "ghcr.io/kowan-lab/kowan:1.4.2"})
        self.assertEqual((st, body), (202, {"id": "id-update"}))
        self.assertEqual(self.mgr.calls[-1], ("start", "server", None, "update", "ghcr.io/kowan-lab/kowan:1.4.2", "admin", True))
        st, body = await self.req("POST", "/system/server/update")  # empty body allowed
        self.assertEqual(st, 202)
        st, body = await self.req("POST", "/system/server/rollback", json={"restoreDb": False})
        self.assertEqual((st, body), (202, {"id": "id-rollback"}))
        self.assertFalse(self.mgr.calls[-1][-1])
        st, _ = await self.req("POST", "/system/server/update", json={"bogus": 1})
        self.assertEqual(st, 400)
        st, _ = await self.req("POST", "/system/server/update", json={"image": 5})
        self.assertEqual(st, 400)

    async def test_conflict_and_validation_errors(self):
        self.mgr.fail = UpdateError("already running", 409)
        st, body = await self.req("POST", "/system/server/update")
        self.assertEqual(st, 409)
        self.assertIn("already running", body["error"])
        self.mgr.fail = UpdateError("not supported")
        st, _ = await self.req("POST", "/hosts/h1/agent/update")
        self.assertEqual(st, 400)

    async def test_history_and_get(self):
        st, body = await self.req("GET", "/system/server/updates?limit=5")
        self.assertEqual(st, 200)
        self.assertEqual([r["id"] for r in body], ["s1"])
        self.assertIsNone(body[0]["hostId"])
        for k in ("phases", "logs", "fromImage", "toVersion", "startedBy", "finishedAt"):
            self.assertIn(k, body[0])
        st, body = await self.req("GET", "/system/updates/a1", role="operator")
        self.assertEqual((st, body["hostId"]), (200, "h1"))
        st, _ = await self.req("GET", "/system/updates/s1", role="operator")
        self.assertEqual(st, 403)
        st, _ = await self.req("GET", "/system/updates/s1", role="viewer")
        self.assertEqual(st, 403)
        st, _ = await self.req("GET", "/system/updates/nope")
        self.assertEqual(st, 404)
        st, _ = await self.req("GET", "/system/server/updates?limit=x")
        self.assertEqual(st, 400)

    async def test_agent_routes_roles(self):
        st, body = await self.req("GET", "/hosts/h1/agent/update", role="operator")
        self.assertEqual(st, 200)
        self.assertEqual(self.mgr.calls[-1], ("status", "agent", "h1"))
        st, _ = await self.req("GET", "/hosts/h1/agent/update", role="viewer")
        self.assertEqual(st, 403)
        st, body = await self.req("GET", "/hosts/h1/agent/updates", role="operator")
        self.assertEqual([r["id"] for r in body], ["a1"])
        st, _ = await self.req("POST", "/hosts/h1/agent/update", role="operator")
        self.assertEqual(st, 403)
        st, body = await self.req("POST", "/hosts/h1/agent/update", json={"image": "ghcr.io/kowan-lab/kowan-agent:2"})
        self.assertEqual((st, body), (202, {"id": "id-update"}))
        st, body = await self.req("POST", "/hosts/h1/agent/rollback")
        self.assertEqual((st, body), (202, {"id": "id-rollback"}))
        st, _ = await self.req("POST", "/hosts/nope/agent/update")
        self.assertEqual(st, 404)

    async def test_rolling_batch(self):
        st, body = await self.req("POST", "/agents/update", json={"hostIds": ["h1", "h2"], "image": "a:2"})
        self.assertEqual((st, body), (202, {"batchId": "b1", "ids": ["i1", "i2"]}))
        self.assertEqual(self.mgr.calls[-1], ("batch", ["h1", "h2"], "a:2", "admin"))
        for payload, code in (({"hostIds": []}, 400), ({"hostIds": ["h1", "h1"]}, 400),
                              ({"hostIds": ["local"]}, 400), ({"hostIds": ["zz"]}, 404),
                              ({"hostIds": ["h1"], "x": 1}, 400)):
            st, _ = await self.req("POST", "/agents/update", json=payload)
            self.assertEqual(st, code, payload)
        st, _ = await self.req("POST", "/agents/update", role="operator", json={"hostIds": ["h1"]})
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
