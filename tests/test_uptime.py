"""Uptime checks end to end: HTTP and TCP probes against real local servers,
the up/down state machine with incidents, alerts and recovery notifications,
the REST API with its validation and roles, and the public status page."""

import asyncio
import datetime
import os
import socket
import ssl
import tempfile
import unittest

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from kowan.uptime import probe
from kowan.uptime.runner import UptimeRunner
from tests.apicase import ServerCase


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tls_context(days: int):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days)).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cpath, kpath = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    with open(cpath, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kpath, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cpath, kpath)
    return ctx, cert.not_valid_after_utc


class Target:
    """A small HTTP(S) server whose answers the test controls."""

    def __init__(self):
        self.status = 200
        self.body = "hello kowan"
        self.delay = 0.0

    async def handler(self, request):
        if self.delay:
            await asyncio.sleep(self.delay)
        return web.Response(status=self.status, text=self.body)

    async def start(self, ssl_ctx=None):
        app = web.Application()
        app.router.add_get("/{tail:.*}", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.port = _free_port()
        await web.TCPSite(self.runner, "127.0.0.1", self.port, ssl_context=ssl_ctx).start()
        scheme = "https" if ssl_ctx else "http"
        self.url = f"{scheme}://127.0.0.1:{self.port}/health"
        return self

    async def stop(self):
        await self.runner.cleanup()


class TestProbes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = await Target().start()

    async def asyncTearDown(self):
        await self.t.stop()

    def test_parsers(self):
        self.assertEqual(probe.parse_expect("200-299, 301"), [(200, 299), (301, 301)])
        for bad in ("abc", "99", "300-200", ","):
            with self.assertRaises(ValueError):
                probe.parse_expect(bad)
        self.assertEqual(probe.parse_tcp_target("db:5432"), ("db", 5432))
        self.assertEqual(probe.parse_tcp_target("[::1]:22"), ("::1", 22))
        for bad in ("db", "db:x", ":80", "db:70000"):
            with self.assertRaises(ValueError):
                probe.parse_tcp_target(bad)
        probe.validate_http_target("https://a.example/x")
        for bad in ("ftp://a", "http://", "nope"):
            with self.assertRaises(ValueError):
                probe.validate_http_target(bad)

    async def test_http_probe(self):
        ok = await probe.http_probe(self.t.url, method="GET", expect="200", keyword="kowan", verify_tls=True, timeout=5)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.code, 200)
        self.assertGreater(ok.latency_ms, 0)
        miss = await probe.http_probe(self.t.url, method="GET", expect="200", keyword="absent", verify_tls=True, timeout=5)
        self.assertEqual(miss.error, "keyword 'absent' not found")
        self.t.status = 503
        bad = await probe.http_probe(self.t.url, method="HEAD", expect="200-399", keyword="", verify_tls=True, timeout=5)
        self.assertEqual((bad.ok, bad.code, bad.error), (False, 503, "HTTP 503"))
        self.t.status, self.t.delay = 200, 1.0
        slow = await probe.http_probe(self.t.url, method="GET", expect="200", keyword="", verify_tls=True, timeout=0.2)
        self.assertIn("timed out", slow.error)
        gone = await probe.http_probe(f"http://127.0.0.1:{_free_port()}/", method="GET", expect="200", keyword="",
                                      verify_tls=True, timeout=2)
        self.assertFalse(gone.ok)
        self.assertTrue(gone.error)

    async def test_tcp_probe(self):
        ok = await probe.tcp_probe(f"127.0.0.1:{self.t.port}", timeout=2)
        self.assertTrue(ok.ok)
        refused = await probe.tcp_probe(f"127.0.0.1:{_free_port()}", timeout=2)
        self.assertFalse(refused.ok)
        self.assertTrue(refused.error)

    async def test_https_and_certificate_expiry(self):
        ctx, not_after = _tls_context(days=20)
        t = await Target().start(ssl_ctx=ctx)
        try:
            strict = await probe.http_probe(t.url, method="GET", expect="200", keyword="", verify_tls=True, timeout=5)
            self.assertFalse(strict.ok)  # self-signed
            lax = await probe.http_probe(t.url, method="GET", expect="200", keyword="", verify_tls=False, timeout=5)
            self.assertTrue(lax.ok)
            exp = await probe.cert_expiry(t.url, timeout=5)
            self.assertEqual(exp, not_after.strftime("%Y-%m-%dT%H:%M:%SZ"))
        finally:
            await t.stop()
        self.assertIsNone(await probe.cert_expiry("http://127.0.0.1/", timeout=1))
        self.assertIsNone(await probe.cert_expiry(f"https://127.0.0.1:{_free_port()}/", timeout=1))
        self.assertIsNone(probe._not_after(b"not a certificate"))


class UptimeApiCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.t = await Target().start()
        self.events = self.svc.bus.subscribe("alert")
        # The tests drive the probes; the server's own runner would race them
        # (a new check runs on its next tick, within a second).
        await self.svc.uptime.stop()

    async def asyncTearDown(self):
        await self.t.stop()
        await super().asyncTearDown()

    async def create(self, **body):
        base = {"name": "web", "kind": "http", "target": self.t.url, "failThreshold": 2}
        _, c = await self.call("POST", "/uptime/checks", "operator", json={**base, **body}, expect=201)
        return c

    async def probe_now(self, check_id):
        _, r = await self.call("POST", f"/uptime/checks/{check_id}/run", "operator", expect=200)
        return r

    async def next_event(self):
        return await asyncio.wait_for(self.events.__aiter__().__anext__(), 2)


class TestUptimeApi(UptimeApiCase):
    async def test_down_and_up_with_alerts_incidents_and_notifications(self):
        await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "type": "webhook", "name": "ops", "enabled": True, "minLevel": "warning",
            "config": {"url": "http://127.0.0.1:9/hook"}})
        _, chans = await self.call("GET", "/credentials", "admin", params={"kind": "channel"})
        c = await self.create(channelId=chans[0]["id"], level="warning")
        self.assertEqual(c["status"], "unknown")
        r = await self.probe_now(c["id"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"]["status"], "up")

        self.t.status = 500
        r = await self.probe_now(c["id"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["check"]["status"], "up")  # one failure is below the threshold
        r = await self.probe_now(c["id"])
        self.assertEqual(r["check"]["status"], "down")
        ev = await self.next_event()
        self.assertEqual(ev.payload["title"], "Down: web")
        self.assertEqual(ev.payload["level"], "warning")
        self.assertEqual(ev.payload["channelId"], chans[0]["id"])
        _, alerts = await self.call("GET", "/alerts", "viewer")
        self.assertFalse(alerts[0]["resolved"])
        _, inc = await self.call("GET", "/uptime/incidents", "viewer", params={"check": c["id"]})
        self.assertEqual(inc[0]["endedAt"], "")
        self.assertEqual(inc[0]["error"], "HTTP 500")

        self.t.status = 200
        r = await self.probe_now(c["id"])
        self.assertEqual(r["check"]["status"], "up")
        ev = await self.next_event()
        self.assertEqual(ev.payload["title"], "Up again: web")
        self.assertTrue(ev.payload["resolved"])
        _, alerts = await self.call("GET", "/alerts", "viewer")
        self.assertTrue(alerts[0]["resolved"])
        _, inc = await self.call("GET", "/uptime/incidents", "viewer")
        self.assertNotEqual(inc[0]["endedAt"], "")

        _, got = await self.call("GET", f"/uptime/checks/{c['id']}", "viewer", expect=200)
        self.assertAlmostEqual(got["uptime24h"], 50.0)
        self.assertEqual(len(got["recent"]), 4)
        self.assertIsNotNone(got["latency24h"])
        _, results = await self.call("GET", f"/uptime/checks/{c['id']}/results", "viewer", params={"hours": "1"}, expect=200)
        self.assertEqual([x["ok"] for x in results], [True, False, False, True])
        await self.call("GET", f"/uptime/checks/{c['id']}/results", "viewer", params={"hours": "x"}, expect=400)

    async def test_tcp_check_and_pause_while_down(self):
        c = await self.create(name="db", kind="tcp", target=f"127.0.0.1:{_free_port()}", failThreshold=1)
        r = await self.probe_now(c["id"])
        self.assertEqual(r["check"]["status"], "down")
        await self.next_event()
        # Pausing a check that is down closes its outage and resolves the alert.
        _, paused = await self.call("PUT", f"/uptime/checks/{c['id']}", "operator", json={"enabled": False}, expect=200)
        self.assertEqual(paused["status"], "paused")
        _, inc = await self.call("GET", "/uptime/incidents", "operator")
        self.assertNotEqual(inc[0]["endedAt"], "")
        _, alerts = await self.call("GET", "/alerts", "viewer")
        self.assertTrue(alerts[0]["resolved"])

    async def test_validation_roles_and_delete(self):
        await self.roles("POST", "/uptime/checks", "operator", json={"name": "x", "target": self.t.url})
        bad = [
            {"name": "", "target": self.t.url},
            {"name": "a", "kind": "icmp", "target": self.t.url},
            {"name": "a", "target": "ftp://x"},
            {"name": "a", "kind": "tcp", "target": "db"},
            {"name": "a", "target": self.t.url, "method": "POST"},
            {"name": "a", "target": self.t.url, "expectStatus": "abc"},
            {"name": "a", "target": self.t.url, "intervalSec": 5},
            {"name": "a", "target": self.t.url, "intervalSec": "x"},
            {"name": "a", "target": self.t.url, "intervalSec": 20, "timeoutSec": 30},
            {"name": "a", "target": self.t.url, "level": "panic"},
            {"name": "a", "target": self.t.url, "channelId": "nope"},
            {"name": "a", "target": self.t.url, "deploymentId": "nope"},
            {"name": "a", "target": self.t.url, "extra": 1},
        ]
        for body in bad:
            await self.call("POST", "/uptime/checks", "operator", json=body, expect=400)
        await self.call("POST", "/uptime/checks", "operator", json=[1], expect=400)
        c = await self.create(target=self.t.url.replace("http://", "http://user:pw@"))
        await self.call("POST", "/uptime/checks", "operator", json={"name": "web", "target": self.t.url}, expect=409)
        other = await self.create(name="other")
        await self.call("PUT", f"/uptime/checks/{other['id']}", "operator", json={"name": "web"}, expect=409)
        # Viewers do not see credentials in the target.
        _, lst = await self.call("GET", "/uptime/checks", "viewer")
        self.assertNotIn("pw", lst[1]["target"] if lst[1]["name"] == "web" else lst[0]["target"])
        await self.roles("DELETE", f"/uptime/checks/{c['id']}", "operator")
        await self.call("DELETE", f"/uptime/checks/{c['id']}", "operator", expect=204)
        await self.call("GET", f"/uptime/checks/{c['id']}", "viewer", expect=404)
        await self.call("POST", f"/uptime/checks/{c['id']}/run", "operator", expect=404)
        await self.call("GET", "/uptime/incidents", "viewer", params={"check": "nope"}, expect=404)

    async def test_delete_while_down_resolves_the_alert(self):
        c = await self.create(failThreshold=1)
        self.t.status = 500
        await self.probe_now(c["id"])
        await self.call("DELETE", f"/uptime/checks/{c['id']}", "operator", expect=204)
        _, alerts = await self.call("GET", "/alerts", "viewer")
        self.assertTrue(alerts[0]["resolved"])

    async def test_status_page(self):
        r = await self.client.get("/api/v1/public/status")
        self.assertEqual(r.status, 404)
        await self.roles("PUT", "/uptime/status-page", "admin", json={"enabled": True})
        await self.call("PUT", "/uptime/status-page", "admin", json={"enabled": True, "title": "x" * 81}, expect=400)
        await self.call("PUT", "/uptime/status-page", "admin", json={"enabled": True, "description": "x" * 501}, expect=400)
        await self.call("PUT", "/uptime/status-page", "admin", json={"nope": 1}, expect=400)
        await self.call("PUT", "/uptime/status-page", "admin", json=[1], expect=400)
        _, page = await self.call("PUT", "/uptime/status-page", "admin",
                                  json={"enabled": True, "title": "Acme status", "description": "All of Acme"}, expect=200)
        self.assertEqual(page["title"], "Acme status")
        _, page = await self.call("GET", "/uptime/status-page", "admin", expect=200)
        self.assertTrue(page["enabled"])

        r = await self.client.get("/api/v1/public/status")
        self.assertEqual((await r.json())["overall"], "unknown")
        pub = await self.create(name="site", public=True, failThreshold=1)
        await self.create(name="internal")  # not public
        await self.probe_now(pub["id"])
        r = await self.client.get("/api/v1/public/status")  # no Authorization header
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=30")
        data = await r.json()
        self.assertEqual(data["title"], "Acme status")
        self.assertEqual(data["overall"], "up")
        self.assertEqual([c["name"] for c in data["checks"]], ["site"])
        self.assertNotIn("target", data["checks"][0])
        self.assertEqual(data["checks"][0]["days"][0]["uptime"], 100.0)
        self.t.status = 500
        await self.probe_now(pub["id"])
        data = await (await self.client.get("/api/v1/public/status")).json()
        self.assertEqual(data["overall"], "down")
        self.assertEqual(data["incidents"][0]["name"], "site")
        self.assertNotIn("error", data["incidents"][0])
        self.assertIsNone(data["checks"][0]["latencyMs"])


class TestRunner(UptimeApiCase):
    async def test_tick_schedules_due_checks_and_prunes(self):
        runner = UptimeRunner(self.svc, retention=datetime.timedelta(days=1))
        c = await self.create()
        await self.create(name="off", enabled=False)
        runner.tick()
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.svc.store.list_uptime_results(c["id"], "0")), 1)
        runner.tick()  # not due yet: nothing new
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.svc.store.list_uptime_results(c["id"], "0")), 1)
        runner.reschedule(c["id"])
        runner.tick()
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.svc.store.list_uptime_results(c["id"], "0")), 2)
        # Old results go.
        self.svc.store.add_uptime_result(c["id"], "2000-01-01T00:00:00Z", True, 1, 200, "")
        runner.prune()
        self.assertEqual(len(self.svc.store.list_uptime_results(c["id"], "0")), 2)
        runner.start()
        runner.start()  # idempotent
        await runner.stop()
        await runner.stop()

    async def test_a_failing_check_does_not_stop_the_loop(self):
        runner = UptimeRunner(self.svc)
        c = await self.create()
        self.svc.store.delete_uptime_check(c["id"])  # vanishes between listing and probing
        runner._running.add("ghost")
        await runner._guarded("ghost")
        self.assertNotIn("ghost", runner._running)

    async def test_https_check_records_the_certificate(self):
        ctx, not_after = _tls_context(days=40)
        t = await Target().start(ssl_ctx=ctx)
        try:
            c = await self.create(name="tls", target=t.url, verifyTls=False)
            r = await self.probe_now(c["id"])
            self.assertEqual(r["check"]["certExpiresAt"], not_after.strftime("%Y-%m-%dT%H:%M:%SZ"))
        finally:
            await t.stop()


if __name__ == "__main__":
    unittest.main()
