"""Retained logs: timestamp normalisation and query parsing, the archive
(search, paging, retention, pruning), the collector following containers of
workloads that keep logs (fake Engine), and the REST API with its roles,
validation and team scoping."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from kowan.logstore.collector import Collector, split_line
from kowan.workloads import parse_workload, workload_of
from kowan.logstore.store import LogStore, fts_query, normalize_ts
from tests.apicase import ServerCase


def ts(sec: int, frac: str = "") -> str:
    return f"2026-10-20T10:00:{sec:02d}" + (f".{frac}" if frac else "") + "Z"


class TestHelpers(unittest.TestCase):
    def test_normalize_ts(self):
        self.assertEqual(normalize_ts("2026-10-20T10:00:00Z"), "2026-10-20T10:00:00.000000000Z")
        self.assertEqual(normalize_ts("2026-10-20T10:00:00.5Z"), "2026-10-20T10:00:00.500000000Z")
        self.assertEqual(normalize_ts("2026-10-20T10:00:00.123456789123Z"), "2026-10-20T10:00:00.123456789Z")
        self.assertEqual(normalize_ts("2026-10-20T12:00:00+02:00"), "2026-10-20T10:00:00.000000000Z")
        self.assertEqual(normalize_ts("2026-10-20T10:00:00"), "2026-10-20T10:00:00.000000000Z")
        for bad in ("", "yesterday", "2026-10-20 10:00:00Z"):
            with self.assertRaises(ValueError):
                normalize_ts(bad)

    def test_fts_query(self):
        self.assertEqual(fts_query(""), ("", []))
        self.assertEqual(fts_query("error db"), ('"error" AND "db"', []))
        self.assertEqual(fts_query('"connection refused"'), ('"connection refused"', []))
        m, ex = fts_query("error -health")
        self.assertEqual(m, '("error") NOT ("health")')
        self.assertEqual(ex, ['"health"'])
        self.assertEqual(fts_query("-health"), ("", ['"health"']))
        # FTS syntax is just text; a lone dash is a word.
        self.assertEqual(fts_query("a OR b*")[0], '"a" AND "OR" AND "b*"')
        self.assertEqual(fts_query('say "hi" "" -')[0], '"say" AND "hi" AND "-"')

    def test_split_line_and_workload(self):
        self.assertEqual(split_line("2026-10-20T10:00:00.1Z hello world"),
                         ("2026-10-20T10:00:00.100000000Z", "hello world"))
        self.assertIsNone(split_line("not-a-time hello"))
        self.assertIsNone(split_line(""))
        self.assertEqual(len(split_line("2026-10-20T10:00:00Z " + "x" * 10000)[1]), 8192)
        self.assertEqual(workload_of({"kowan.deployment.id": "d1"}, "h", "n", {}), ("dep~d1", "", "d1"))
        self.assertEqual(workload_of({"com.docker.compose.project": "shop"}, "h", "n", {("h", "shop"): "d2"}),
                         ("dep~d2", "shop", "d2"))
        self.assertEqual(workload_of({"com.docker.compose.project": "shop"}, "h", "n", {}), ("stack~h~shop", "shop", ""))
        self.assertEqual(workload_of(None, "h", "web", {}), ("ctr~h~web", "", ""))
        # A swarm stack groups like a compose project, and a Kowan swarm
        # deployment (namespace = its resource name) stays its deployment.
        swarm = {"com.docker.stack.namespace": "mon", "com.docker.swarm.service.name": "mon_grafana"}
        self.assertEqual(workload_of(swarm, "h", "mon_grafana.1.x", {}), ("stack~h~mon", "mon", ""))
        self.assertEqual(workload_of(swarm, "h", "mon_grafana.1.x", {("h", "mon"): "d3"}), ("dep~d3", "mon", "d3"))
        self.assertEqual(parse_workload("stack~h~shop"), ("stack", "h", "shop"))
        self.assertEqual(parse_workload("dep~d1"), ("dep", "", "d1"))


class TestLogStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = LogStore(os.path.join(self.dir, "logs.db"))
        self.s.set_retention("ctr~h1~web", 7, "2026-10-20T00:00:00Z", "root")
        self.s.set_retention("stack~h2~shop", 30, "2026-10-20T00:00:00Z", "root")
        self.web, last = self.s.source("h1", "c1", "web", "ctr~h1~web", "", "", {"team": "web"})
        self.assertEqual(last, "")
        self.shop, _ = self.s.source("h2", "c2", "shop-db-1", "stack~h2~shop", "shop", "")
        self.s.append(self.web, [(normalize_ts(ts(1)), "GET /health 200"), (normalize_ts(ts(2)), "error: db timeout"),
                                 (normalize_ts(ts(3)), "GET /api 500 error")])
        self.s.append(self.shop, [(normalize_ts(ts(4)), "connection refused by peer"),
                                  (normalize_ts(ts(5)), "ready to accept connections")])

    def tearDown(self):
        self.s.close()

    def lines(self, **kw):
        return [i["line"] for i in self.s.search(**kw)]

    def test_search(self):
        self.assertEqual(self.lines(), ["ready to accept connections", "connection refused by peer", "GET /api 500 error",
                                        "error: db timeout", "GET /health 200"])
        self.assertEqual(self.lines(text="error"), ["GET /api 500 error", "error: db timeout"])
        self.assertEqual(self.lines(text="error -db"), ["GET /api 500 error"])
        self.assertEqual(self.lines(text='"connection refused"'), ["connection refused by peer"])
        self.assertEqual(self.lines(text="-error -connections"),
                         ["connection refused by peer", "GET /health 200"])
        self.assertEqual(self.lines(workloads=["stack~h2~shop"]), ["ready to accept connections", "connection refused by peer"])
        self.assertEqual(self.lines(workloads=[]), [])
        self.assertEqual(self.lines(host_id="h1", text="GET"), ["GET /api 500 error", "GET /health 200"])
        self.assertEqual(self.lines(since=normalize_ts(ts(2)), until=normalize_ts(ts(4))),
                         ["connection refused by peer", "GET /api 500 error", "error: db timeout"])
        page = self.s.search(limit=2)
        self.assertEqual(len(page), 2)
        self.assertEqual([i["line"] for i in self.s.search(before=page[-1]["id"], limit=2)],
                         ["GET /api 500 error", "error: db timeout"])
        item = self.s.search(text="health")[0]
        self.assertEqual((item["hostId"], item["container"], item["workload"], item["labels"]),
                         ("h1", "web", "ctr~h1~web", {"team": "web"}))
        self.assertEqual(item["ts"], "2026-10-20T10:00:01.000000000Z")

    def test_retention(self):
        rows = {r["workload"]: r for r in self.s.list_retention()}
        self.assertEqual(rows["ctr~h1~web"]["lines"], 3)
        self.assertEqual(rows["ctr~h1~web"]["oldest"], normalize_ts(ts(1)))
        self.assertEqual(rows["ctr~h1~web"]["createdBy"], "root")
        self.s.set_retention("ctr~h1~web", 14, "later", "someone")  # update keeps the author
        self.assertEqual(self.s.retention()["ctr~h1~web"], 14)
        self.assertEqual({r["workload"]: r["createdBy"] for r in self.s.list_retention()}["ctr~h1~web"], "root")
        # The source row remembers the last stored time, for resuming.
        self.assertEqual(self.s.source("h1", "c1", "web", "ctr~h1~web", "", "")[1], normalize_ts(ts(3)))
        self.assertEqual(self.s.delete_retention("ctr~h1~web"), 3)
        self.assertEqual(self.lines(text="GET"), [])
        self.assertEqual([r["workload"] for r in self.s.list_retention()], ["stack~h2~shop"])
        self.assertEqual(self.s.append(self.shop, []), 0)

    def test_prune(self):
        now = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)  # 5 days after the lines
        self.assertEqual(self.s.prune(now, 1000), 0)
        self.s.set_retention("ctr~h1~web", 1, "", "")
        self.assertEqual(self.s.prune(now, 1000), 3)  # older than a day
        self.assertEqual(len(self.lines()), 2)
        # The cap drops the oldest lines first.
        self.s.append(self.shop, [(normalize_ts(ts(6)), "one"), (normalize_ts(ts(7)), "two")])
        self.assertEqual(self.s.prune(now, 3), 1)
        self.assertEqual(self.lines(), ["two", "one", "ready to accept connections"])
        # Lines of a workload that no longer keeps logs go.
        self.s._x("DELETE FROM log_retention WHERE workload = 'stack~h2~shop'")
        self.assertEqual(self.s.prune(now, 1000), 3)
        self.assertEqual(self.lines(), [])
        # With no policy at all, everything goes (and the FTS index follows).
        src, _ = self.s.source("h3", "c3", "x", "ctr~h3~x", "", "")
        self.s.append(src, [(normalize_ts(ts(1)), "orphan")])
        self.s._x("DELETE FROM log_retention")
        self.assertEqual(self.s.prune(now, 1000), 1)
        self.assertEqual(self.lines(text="orphan"), [])


class _Stream:
    """An async iterator of chunks, like a docker log stream."""

    def __init__(self, chunks, hold=False):
        self.chunks = list(chunks)
        self.hold = hold
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.hold:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _Ops:
    def __init__(self, stream):
        self.stream = stream
        self.calls = []

    async def container_logs(self, cid, timestamps, tail):
        self.calls.append((cid, timestamps, tail))
        return self.stream


class TestFollow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = LogStore(os.path.join(tempfile.mkdtemp(), "logs.db"))
        self.c = Collector(object(), self.s)

    async def asyncTearDown(self):
        self.s.close()

    async def test_follow_splits_chunks_and_resumes(self):
        stream = _Stream([b"2026-10-20T10:00:01Z first\n2026-10-20T10:00:02Z sec", b"ond\r\n", b"garbage line\n",
                          "2026-10-20T10:00:02Z same second\n", b"2026-10-20T10:00:03Z tail without newline"])
        ops = _Ops(stream)
        n = await self.c.follow(ops, "h", "c1", "web", "ctr~h~web", "", "", {"a": "b"})
        self.assertEqual(n, 4)
        self.assertTrue(stream.closed)
        self.assertEqual(ops.calls, [("c1", True, 200)])
        self.assertEqual([i["line"] for i in self.s.search()], ["tail without newline", "same second", "second", "first"])
        # A restart re-reads a longer tail and skips what is stored.
        ops2 = _Ops(_Stream([b"2026-10-20T10:00:02Z second\n2026-10-20T10:00:03Z tail without newline\n"
                             b"2026-10-20T10:00:04Z new\n"]))
        self.assertEqual(await self.c.follow(ops2, "h", "c1", "web", "ctr~h~web", "", ""), 1)
        self.assertEqual(ops2.calls[0][2], 1000)
        self.assertEqual(self.s.search()[0]["line"], "new")


class TestCollectorAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The app's own collector loop is stopped: tests drive sync() themselves.
        await self.svc.log_collector.stop()

    async def asyncTearDown(self):
        await self.svc.log_collector.stop()
        await super().asyncTearDown()

    async def wait_lines(self, n, **kw):
        for _ in range(100):
            items = self.svc.logs.search(**kw)
            if len(items) >= n:
                return items
            await asyncio.sleep(0.02)
        self.fail(f"expected {n} lines, got {len(self.svc.logs.search(**kw))}")

    async def test_collector_follows_workloads_with_retention(self):
        web = self.engine.add_container("web", logs=["hello", "oops"])
        self.engine.log_times[web] = ["2026-10-20T10:00:01Z", "2026-10-20T10:00:02.5Z"]
        shop = self.engine.add_container("shop-db-1", labels={"com.docker.compose.project": "shop"}, logs=["db up"])
        self.engine.add_container("other", logs=["not kept"])
        self.engine.add_container("stopped", state="exited", logs=["stopped"])
        c = self.svc.log_collector
        await c.sync()
        self.assertEqual(c.following, [])
        self.svc.logs.set_retention("ctr~local~web", 7, "", "")
        self.svc.logs.set_retention("stack~local~shop", 7, "", "")
        self.svc.logs.set_retention("ctr~local~stopped", 7, "", "")
        await c.sync()
        self.assertEqual(c.following, sorted([("local", web), ("local", shop)]))
        items = await self.wait_lines(3)
        self.assertEqual({i["line"] for i in items}, {"hello", "oops", "db up"})
        oops = next(i for i in items if i["line"] == "oops")
        self.assertEqual((oops["ts"], oops["container"], oops["workload"]),
                         ("2026-10-20T10:00:02.500000000Z", "web", "ctr~local~web"))
        self.assertEqual(next(i for i in items if i["line"] == "db up")["stack"], "shop")
        # The fake log stream ends: the next sync restarts the follower, which
        # skips the lines already stored.
        await asyncio.sleep(0.05)
        await c.sync()
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.svc.logs.search()), 3)
        # Dropping the policy stops the follower.
        self.svc.logs.delete_retention("stack~local~shop")
        await c.sync()
        self.assertNotIn(("local", shop), c.following)

    async def test_compose_deployment_workload(self):
        _, dep = await self.call("POST", "/deployments", json={
            "name": "shop", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": "services:\n  web:\n    image: nginx:1.27\n"}, expect=201)
        self.engine.add_container("shop-web-1", labels={"com.docker.compose.project": "shop"}, logs=["hi"])
        await self.call("PUT", f"/log-archive/retention/dep~{dep['id']}", "operator", json={"days": 3}, expect=200)
        items = await self.wait_lines(1)
        self.assertEqual((items[0]["workload"], items[0]["deploymentId"]), (f"dep~{dep['id']}", dep["id"]))

    async def test_api(self):
        await self.roles("PUT", "/log-archive/retention/ctr~local~web", "operator", json={"days": 7})
        await self.roles("DELETE", "/log-archive/retention/ctr~local~web", "operator")
        for bad in ("web", "dep~", "stack~local", "ctr~~web", "ctr~a~b~c", "nope~x"):
            await self.call("PUT", f"/log-archive/retention/{bad}", json={"days": 7}, expect=400)
        for body in ({"days": 0}, {"days": 366}, {"days": "x"}, {}, {"days": 7, "extra": 1}, ["x"]):
            await self.call("PUT", "/log-archive/retention/ctr~local~web", json=body, expect=400)
        web = self.engine.add_container("web", logs=["GET / 200", "boom error"])
        self.engine.log_times[web] = ["2026-10-20T10:00:01Z", "2026-10-20T10:00:02Z"]
        _, row = await self.call("PUT", "/log-archive/retention/ctr~local~web", "operator", json={"days": 7}, expect=200)
        self.assertEqual((row["workload"], row["days"], row["createdBy"]), ("ctr~local~web", 7, "operator"))
        await self.wait_lines(2)
        _, rows = await self.call("GET", "/log-archive/retention", "viewer", expect=200)
        self.assertEqual(rows[0]["lines"], 2)

        _, res = await self.call("GET", "/log-archive/search", "viewer", params={"q": "error"}, expect=200)
        self.assertEqual([i["line"] for i in res["items"]], ["boom error"])
        self.assertEqual(res["items"][0]["hostName"], "local")
        self.assertNotIn("labels", res["items"][0])
        self.assertIsNone(res["next"])
        _, res = await self.call("GET", "/log-archive/search", params={"limit": "1"}, expect=200)
        self.assertEqual([i["line"] for i in res["items"]], ["boom error"])
        _, res2 = await self.call("GET", "/log-archive/search", params={"limit": "1", "before": str(res["next"])}, expect=200)
        self.assertEqual([i["line"] for i in res2["items"]], ["GET / 200"])
        _, res = await self.call("GET", "/log-archive/search", params={
            "since": "2026-10-20T10:00:02Z", "until": "2026-10-20T12:00:02+02:00", "workload": "ctr~local~web",
            "host": "local"}, expect=200)
        self.assertEqual([i["line"] for i in res["items"]], ["boom error"])
        _, res = await self.call("GET", "/log-archive/search", params={"hours": "1"}, expect=200)
        self.assertEqual(res["items"], [])  # the lines are from 2026-10-20
        for params in ({"limit": "x"}, {"since": "yesterday"}, {"until": "x"}, {"hours": "x"}, {"workload": "bad"}):
            await self.call("GET", "/log-archive/search", params=params, expect=400)

        _, out = await self.call("DELETE", "/log-archive/retention/ctr~local~web", "operator", expect=200)
        self.assertEqual(out["removed"], 2)
        await self.call("PUT", "/log-archive/retention/ctr~local~nothing", json={"days": 1}, expect=200)

    async def test_team_scoping(self):
        _, users = await self.call("GET", "/users")
        uid = next(u["id"] for u in users if u["username"] == "viewer")
        _, team = await self.call("POST", "/teams", json={"name": "web"}, expect=201)
        await self.call("POST", f"/teams/{team['id']}/members", json={"userId": uid}, expect=201)
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": team["id"], "scope": "labels",
                                                  "target": "team=web", "level": "read"}, expect=201)
        src_web, _ = self.svc.logs.source("local", "c-web", "web", "ctr~local~web", "", "", {"team": "web"})
        src_db, _ = self.svc.logs.source("local", "c-db", "db", "ctr~local~db", "", "", {"team": "db"})
        # Many invisible lines between the visible ones: the API reads on
        # until the page is full.
        self.svc.logs.append(src_web, [(normalize_ts(ts(1)), "web old")])
        self.svc.logs.append(src_db, [(normalize_ts(ts(2)), f"db {i}") for i in range(30)])
        self.svc.logs.append(src_web, [(normalize_ts(ts(3)), "web new")])
        _, res = await self.call("GET", "/log-archive/search", "viewer", params={"limit": "5"}, expect=200)
        self.assertEqual([i["line"] for i in res["items"]], ["web new", "web old"])
        self.assertIsNone(res["next"])
        _, res = await self.call("GET", "/log-archive/search", params={"limit": "5"}, expect=200)
        self.assertEqual(len(res["items"]), 5)
        self.assertIsNotNone(res["next"])


if __name__ == "__main__":
    unittest.main()
