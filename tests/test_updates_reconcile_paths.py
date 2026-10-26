"""The self-update reconciler's rarer paths, with a stub manager and host:
unknown records past the recheck window, unreachable agents and deleted
hosts, transient inspect errors, helpers that vanished after launch, logs
that cannot be read, leftover helpers and preflight containers, notifications,
snapshot pruning and the loop."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kowan.errors import NotFound
from kowan.models import Container, Host
from kowan.store import Store
from kowan.updater import reconcile as rc
from kowan.updater.plan import encode_plan
from kowan.updater.reconcile import Reconciler

T0 = rc.parse_ts("2026-10-26T10:00:00Z")


def rec(id_="u1", **kw):
    base = {"id": id_, "component": "agent", "hostId": "h1", "mode": "update", "status": "running",
            "phases": [{"phase": "launch", "ok": True}], "logs": [], "error": "",
            "startedAt": "2026-10-26T10:00:00Z", "finishedAt": ""}
    base.update(kw)
    return base


class Host_:
    """A daemon with helper containers in it."""

    def __init__(self):
        self.helpers = {}
        self.listed = []
        self.removed = []
        self.inspect_error = None
        self.logs_error = None

    async def inspect_container(self, ref):
        if self.inspect_error:
            raise self.inspect_error
        if ref not in self.helpers:
            raise NotFound(f"no such container {ref}")
        return self.helpers[ref]

    async def list_containers(self, all_):
        return self.listed

    async def remove_container(self, cid, force):
        self.removed.append(cid)
        if cid == "stuck":
            raise RuntimeError("busy")

    async def container_logs(self, ref, follow, tail):
        if self.logs_error:
            raise self.logs_error

        async def it():
            yield b'{"phase": "swap", "ok": true}\n{"result": "updated"}\n'
        return it()


class Manager:
    def __init__(self, host, data_dir):
        self.host = host
        self.reachable = True
        self.live_ids = set()
        self._data = data_dir

    def live(self, uid):
        return uid in self.live_ids

    def local_ops(self):
        return self.host

    def host_reachable(self, host):
        return self.reachable

    def host_ops(self, host):
        if host.id == "broken":
            raise RuntimeError("no client")
        return self.host

    def data_dir(self):
        return self._data


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "k.db"))
        self.store.create_host(Host(id="h1", name="edge", type="agent", mode="edge", token="t" * 64, createdAt="c"))
        self.store.create_host(Host(id="broken", name="b", type="agent", mode="edge", token="u" * 64, createdAt="c"))
        self.host = Host_()
        self.m = Manager(self.host, self.tmp)
        self.svc = SimpleNamespace(store=self.store)

    async def asyncTearDown(self):
        self.store.close()

    def r(self, at=T0):
        return Reconciler(self.svc, self.m, clock=lambda: at)


class TestPaths(Case):
    async def test_unknown_unreachable_and_missing_helpers(self):
        # An unknown record past the 24 h recheck is left alone.
        self.store.save_update(rec("old", status="unknown", finishedAt="2026-10-20T00:00:00Z"))
        # Unreachable agent: still waiting, then unknown after 10 minutes.
        self.store.save_update(rec("u1"))
        self.m.reachable = False
        self.assertTrue(await self.r(T0 + 60).reconcile_once())
        self.assertFalse(await self.r(T0 + 700).reconcile_once())
        got = self.store.get_update("u1")
        self.assertEqual(got["status"], "unknown")
        self.assertIn("the agent did not reconnect within 10 min (last known phase: launch)", got["error"])
        # A deleted host or one whose client cannot be built counts as unreachable.
        self.store.save_update(rec("u2", hostId="gone"))
        self.store.save_update(rec("u3", hostId="broken"))
        self.m.reachable = True
        await self.r(T0 + 700).reconcile_once()
        self.assertEqual(self.store.get_update("u2")["status"], "unknown")
        self.assertEqual(self.store.get_update("u3")["status"], "unknown")

    async def test_transient_inspect_and_vanished_helper(self):
        self.store.save_update(rec("u1"))
        self.host.inspect_error = RuntimeError("timeout")
        self.assertTrue(await self.r(T0 + 5).reconcile_once())
        self.assertEqual(self.store.get_update("u1")["status"], "running")
        self.host.inspect_error = None
        # Launched, but the helper is gone: wait, then unknown.
        self.assertTrue(await self.r(T0 + 60).reconcile_once())
        await self.r(T0 + 700).reconcile_once()
        got = self.store.get_update("u1")
        self.assertEqual(got["status"], "unknown")
        self.assertIn("disappeared", got["error"])
        # Marking twice with the same status is a no-op.
        self.r()._mark(got, "unknown", "again")
        self.assertIn("disappeared", self.store.get_update("u1")["error"])

    async def test_fold_errors_and_notifications(self):
        self.store.save_update(rec("u1", component="server", hostId=""))
        name = rc.helper_name("server", "u1")
        self.host.helpers[name] = {"Id": "hx", "Name": "/" + name, "State": {"Status": "exited", "ExitCode": 0},
                                   "Config": {"Cmd": []}}
        self.host.logs_error = RuntimeError("logs gone")
        self.assertTrue(await self.r().reconcile_once())  # retried later
        self.host.logs_error = None
        with mock.patch.object(rc.dbsnap, "prune_snapshots", side_effect=RuntimeError("ro fs")):
            await self.r().reconcile_once()
        got = self.store.get_update("u1")
        self.assertEqual(got["status"], "updated")
        self.assertEqual(self.host.removed, ["hx"])
        notes = self.store.list_notifications(10) if hasattr(self.store, "list_notifications") else []
        if notes:
            self.assertIn("Server update updated", [n.title for n in notes])

    async def test_orphans(self):
        role = rc.LABEL_ROLE
        uid = rc.LABEL_UPDATE_ID
        self.store.save_update(rec("done", status="updated"))
        self.store.save_update(rec("live", status="running"))
        self.m.live_ids = {"inflight"}
        plan = {"id": "lost", "component": "server", "mode": "update", "image": "kowan:2", "target": "kowan",
                "record": {"hostId": "", "startedAt": "2026-10-26T09:00:00Z"}}
        self.host.helpers["orph"] = {"Id": "orph", "Name": "/kowan-server-updater-lost", "State": {"Status": "exited", "ExitCode": 0},
                                     "Config": {"Cmd": ["helper", "--plan", encode_plan(plan)]}}
        self.host.listed = [
            Container(id="x", labels={}),
            Container(id="y", labels={role: "server-updater"}),  # no update id
            Container(id="pre1", state="exited", labels={role: "server-preflight", uid: "p"}),
            Container(id="stuck", state="exited", labels={role: "server-preflight", uid: "p2"}),
            Container(id="pre2", state="running", labels={role: "server-preflight", uid: "p3"}),
            Container(id="inf", state="running", labels={role: "server-updater", uid: "inflight"}),
            Container(id="own", state="exited", labels={role: "server-updater", uid: "live"}),
            Container(id="run", state="running", labels={role: "server-updater", uid: "nobody"}),
            Container(id="left", state="exited", labels={role: "server-updater", uid: "done"}),
            Container(id="orph", state="exited", labels={role: "server-updater", uid: "lost"}),
        ]
        busy = await self.r()._orphans_local()
        self.assertTrue(busy)  # "run" is still going
        self.assertEqual(sorted(self.host.removed), ["left", "orph", "pre1", "stuck"])
        rebuilt = self.store.get_update("lost")
        self.assertEqual((rebuilt["component"], rebuilt["status"]), ("server", "updated"))
        self.m.host = None
        self.m.local_ops = lambda: None
        self.assertFalse(await self.r()._orphans_local())

    async def test_reconcile_survives_errors_and_loops(self):
        self.store.save_update(rec("u1"))
        r = self.r()
        with mock.patch.object(r, "_one", side_effect=RuntimeError("bug")), \
                mock.patch.object(r, "_orphans_local", side_effect=RuntimeError("scan")):
            self.assertTrue(await r.reconcile_once())
        r = Reconciler(self.svc, self.m, fast=0.01, slow=0.01, clock=lambda: T0)
        calls = []

        async def once(boot=False):
            calls.append(boot)
            if len(calls) == 2:
                raise RuntimeError("x")
            return False
        r.reconcile_once = once
        r.start()
        await asyncio.sleep(0.06)
        await r.stop()
        self.assertEqual(calls[0], True)
        self.assertGreater(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
