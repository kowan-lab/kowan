"""The periodic digest: settings and slots, the text, the sections built
from real store data (updates, CVEs, failed backups, certificates from uptime
checks and exposed domains, open alerts), sending to channels, the schedule
and the API."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

from kowan import digest
from kowan.models import Alert
from tests.apicase import ServerCase

NOW = datetime(2026, 10, 26, 8, 5, tzinfo=timezone.utc)  # a Monday


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestSettingsAndText(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(digest.validate_settings({}, []), digest.DEFAULT_SETTINGS)
        s = digest.validate_settings({"every": "daily", "hour": 7, "channels": ["c1"], "sections": {"cves": False}}, ["c1"])
        self.assertEqual((s["every"], s["hour"], s["channels"], s["sections"]["cves"], s["sections"]["updates"]),
                         ("daily", 7, ["c1"], False, True))
        for bad, msg in (({"every": "monthly"}, "every"), ({"hour": -1}, "hour"), ({"weekday": 9}, "weekday"),
                         ({"enabled": 1}, "enabled"), ({"sections": {"x": True}}, "sections"), ({"sections": []}, "sections"),
                         ({"sections": {"cves": "no"}}, "sections values"), ({"channels": ["nope"]}, "channels"),
                         ({"channels": "c1"}, "channels"), ({"foo": 1}, "unknown")):
            with self.assertRaisesRegex(ValueError, msg):
                digest.validate_settings(bad, ["c1"])

    def test_due(self):
        s = dict(digest.DEFAULT_SETTINGS, enabled=True)  # weekly, Monday 08:00
        self.assertTrue(digest.due(s, "", NOW))
        self.assertFalse(digest.due(s, ts(NOW - timedelta(minutes=5)), NOW))
        self.assertTrue(digest.due(s, ts(NOW - timedelta(days=7)), NOW))
        self.assertFalse(digest.due(s, "", NOW + timedelta(days=1)))
        self.assertFalse(digest.due(s, "", NOW + timedelta(hours=1)))
        self.assertTrue(digest.due(dict(s, every="daily"), "", NOW + timedelta(days=1)))
        self.assertFalse(digest.due(dict(s, enabled=False), "", NOW))
        self.assertEqual(digest.period(dict(s, every="daily")), timedelta(days=1))

    def test_render(self):
        msg = digest.render({"sections": {"updates": [], "alerts": []}})
        self.assertEqual((msg.title, msg.level), ("Kowan digest: all clear", "info"))
        upd = [{"container": f"c{i}", "host": "local", "image": "nginx:1.27"} for i in range(12)]
        msg = digest.render({"sections": {"updates": upd, "alerts": [{"level": "warning", "title": "Disk", "since": "x"}]}})
        self.assertEqual(msg.title, "Kowan digest: 12 updates, 1 open alert")
        self.assertEqual(msg.level, "info")
        self.assertIn("…and 2 more", msg.body)
        self.assertIn("\n\nAlerts still open (1)", msg.body)
        msg = digest.render({"sections": {
            "cves": [{"image": "a", "host": "h", "critical": 0, "high": 3, "fixable": 1}],
            "certificates": [{"name": "shop.example.com", "where": "uptime", "daysLeft": -2}, {"name": "b", "where": "x", "daysLeft": 1}],
            "backups": [{"job": "nightly", "host": "h", "at": "t", "error": ""}]}})
        self.assertEqual(msg.level, "warning")
        self.assertIn("expired 2 days ago", msg.body)
        self.assertIn("1 days left", msg.body)
        self.assertIn("1 vulnerable image, 1 failed backup, 2 certificates expiring", msg.title)
        crit = digest.render({"sections": {"cves": [{"image": "a", "host": "h", "critical": 1, "high": 0, "fixable": 0}]}})
        self.assertEqual(crit.level, "warning")


class _Provider:
    def __init__(self, sink, fail=False):
        self.sink, self.fail = sink, fail

    async def send(self, msg):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sink.append(msg)


class TestDigestAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.svc.digest.stop()
        st = self.svc.store
        st.upsert_image_scan({"imageId": "sha256:a", "imageRef": "nginx:1.20", "hostId": "local",
                              "counts": {"critical": 2, "high": 5}, "fixable": 4, "scannedAt": ts(NOW)})
        st.upsert_image_scan({"imageId": "sha256:b", "imageRef": "alpine:3.22", "hostId": "local", "counts": {"low": 1}})
        st.upsert_image_scan({"imageId": "sha256:c", "imageRef": "broken", "status": "error", "counts": {"critical": 9}})
        st.save_backup_run({"jobName": "nightly", "hostId": "local", "status": "failed", "error": "repo locked\nmore",
                            "startedAt": ts(NOW - timedelta(days=1)), "finishedAt": ts(NOW - timedelta(days=1))})
        st.save_backup_run({"jobName": "old", "hostId": "local", "status": "failed", "startedAt": ts(NOW - timedelta(days=30))})
        st.save_backup_run({"jobName": "fine", "hostId": "local", "status": "success", "startedAt": ts(NOW)})
        st.create_alert(Alert(id=str(uuid.uuid4()), ruleId="r", level="critical", title="Disk almost full", createdAt=ts(NOW)))
        st.create_alert(Alert(id=str(uuid.uuid4()), ruleId="r", level="info", title="done", resolved=True, createdAt=ts(NOW)))
        _, check = await self.call("POST", "/uptime/checks", json={"name": "Shop", "kind": "http", "target": "https://shop.example.com"},
                                   expect=201)
        # The preview runs at the real time, so this expiry is relative to it:
        # 4 full days left, between app.example.com (3) and shop.example.com.
        st.save_uptime_state(check["id"], {"cert_expires_at": ts(datetime.now(timezone.utc) + timedelta(days=5))})
        _, ch = await self.call("POST", "/credentials", json={"kind": "channel", "name": "ops", "type": "webhook", "enabled": True,
                                                              "config": {"url": "https://hooks.example/ops"}}, expect=201)
        _, ch2 = await self.call("POST", "/credentials", json={"kind": "channel", "name": "mail", "type": "webhook", "enabled": True,
                                                               "config": {"url": "https://hooks.example/mail"}}, expect=201)
        self.channels = (ch["id"], ch2["id"])
        self.sent = []

        async def resolve(store, cid, cap):
            return _Provider(self.sent, fail=cid == ch2["id"])

        updates = [{"containerName": "web", "image": "nginx:1.27", "updateAvailable": True},
                   {"containerName": "db", "image": "postgres:16", "updateAvailable": False}]

        async def certs(svc, host):
            return {"domains": [{"domain": "app.example.com", "proxy": "traefik", "certificate": {"daysLeft": 3}},
                                {"domain": "ok.example.com", "proxy": "traefik", "certificate": {"daysLeft": 80}},
                                {"domain": "plain.example.com", "proxy": "traefik", "certificate": None},
                                {"domain": "shop.example.com", "proxy": "traefik", "certificate": {"daysLeft": 9}}]}

        self.patches = [mock.patch("kowan.connections.resolve_service", resolve),
                        mock.patch("kowan.api.updates.check_host", mock.AsyncMock(return_value=updates)),
                        mock.patch("kowan.exposure.service.certificates", certs)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await super().asyncTearDown()

    async def test_preview_settings_and_send(self):
        await self.roles("GET", "/digest/preview", "operator")
        _, d = await self.call("GET", "/digest/preview", "operator", expect=200)
        s = d["sections"]
        self.assertEqual(s["updates"], [{"host": "local", "container": "web", "image": "nginx:1.27"}])
        self.assertEqual([c["image"] for c in s["cves"]], ["nginx:1.20"])
        self.assertEqual([(b["job"], b["error"]) for b in s["backups"]], [("nightly", "repo locked")])
        self.assertEqual([c["name"] for c in s["certificates"]], ["app.example.com", "https://shop.example.com", "shop.example.com"])
        self.assertEqual(s["certificates"][1]["where"], "uptime check Shop")
        self.assertEqual([a["title"] for a in s["alerts"]], ["Disk almost full"])
        self.assertIn("1 update, 1 vulnerable image, 1 failed backup, 3 certificates expiring, 1 open alert", d["title"])
        self.assertEqual(d["level"], "warning")

        await self.roles("GET", "/digest", "operator")
        _, got = await self.call("GET", "/digest", "operator", expect=200)
        self.assertEqual((got["settings"], got["last"]), (digest.DEFAULT_SETTINGS, None))
        await self.roles("PUT", "/digest", "admin", json={})
        _, saved = await self.call("PUT", "/digest", json={"enabled": True, "channels": [self.channels[0]],
                                                           "sections": {"updates": False}}, expect=200)
        self.assertFalse(saved["sections"]["updates"])
        for body in ({"channels": ["nope"]}, {"hour": 99}, ["x"]):
            await self.call("PUT", "/digest", json=body, expect=400)

        await self.roles("POST", "/digest/send", "admin")
        _, rec = await self.call("POST", "/digest/send", expect=200)
        self.assertEqual((rec["sent"], rec["failed"]), (["ops"], []))
        self.assertNotIn("Updates", self.sent[0].body)
        self.assertIn("Images with critical or high vulnerabilities (1)", self.sent[0].body)
        _, got = await self.call("GET", "/digest", expect=200)
        self.assertEqual(got["last"]["sent"], ["ops"])

        # Every enabled channel when none is chosen; a failing one is reported.
        await self.call("PUT", "/digest", json={"channels": []}, expect=200)
        _, rec = await self.call("POST", "/digest/send", expect=200)
        self.assertEqual(rec["sent"], ["ops"])
        self.assertEqual(rec["failed"], ["mail: smtp down"])

    async def test_schedule_and_empty_digests(self):
        st = self.svc.store
        # A disabled channel never gets it.
        _, off = await self.call("POST", "/credentials", json={"kind": "channel", "name": "off", "type": "webhook",
                                                               "config": {"url": "https://hooks.example/off"}}, expect=201)
        self.assertFalse(off.get("enabled", False))  # omitted when off
        sched = self.svc.digest
        self.assertFalse(await sched.tick(NOW))  # off
        digest.save_settings(st, dict(digest.DEFAULT_SETTINGS, enabled=True,
                                      sections={k: k == "backups" for k in digest.SECTIONS}))
        self.assertFalse(await sched.tick(NOW - timedelta(hours=1)))
        self.assertTrue(await sched.tick(NOW))
        self.assertFalse(await sched.tick(NOW))  # same slot
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Failed backups (1)", self.sent[0].body)
        # Next week nothing failed: skipped, unless sendEmpty.
        later = NOW + timedelta(days=7)
        self.assertTrue(await sched.tick(later))
        self.assertTrue(digest.last_sent(st)["skipped"])
        self.assertEqual(len(self.sent), 1)
        digest.save_settings(st, dict(digest.get_settings(st), sendEmpty=True))
        rec = await digest.run(self.svc, later + timedelta(days=7))
        self.assertFalse(rec["skipped"])
        self.assertEqual(self.sent[-1].title, "Kowan digest: all clear")
        # An offline host or a proxy-less host just has nothing to add.
        with mock.patch("kowan.api.updates.check_host", mock.AsyncMock(side_effect=RuntimeError("offline"))), \
                mock.patch("kowan.exposure.service.certificates", mock.AsyncMock(side_effect=RuntimeError("no proxy"))):
            d = await digest.build(self.svc, digest.DEFAULT_SETTINGS, NOW)
        self.assertEqual(d["sections"]["updates"], [])
        self.assertEqual([c["name"] for c in d["sections"]["certificates"]], ["https://shop.example.com"])
        # The loop survives errors.
        with mock.patch.object(digest, "_CHECK_EVERY", 0.01):
            s = digest.Scheduler(self.svc)
            calls = []

            async def bad(now=None):
                calls.append(1)
                raise RuntimeError("x")

            s.tick = bad
            s.start()
            await asyncio.sleep(0.05)
            await s.stop()
            await s.stop()
        self.assertGreater(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
