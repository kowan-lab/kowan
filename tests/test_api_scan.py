"""Image vulnerability scans against the fake Engine: a Trivy container that the
fake "runs" to completion with a JSON report in its logs, the cached/fresh
result, a failing scanner, the per-host listing, the summary and roles. Plus the
report parser on its own."""

import asyncio
import json
import unittest

from kowan.scan import trivy
from tests.apicase import ServerCase

H = "/hosts/local"

REPORT = {
    "ArtifactName": "nginx:1.27",
    "Trivy": {"Version": "0.75.0"},
    "Metadata": {"OS": {"Family": "debian", "Name": "12.8"}, "RepoDigests": ["nginx@sha256:abc"]},
    "Results": [
        {"Target": "nginx:1.27 (debian 12.8)", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2026-0001", "PkgName": "openssl", "InstalledVersion": "3.0.1",
             "FixedVersion": "3.0.2", "Severity": "CRITICAL", "Title": "bad"},
            {"VulnerabilityID": "CVE-2026-0002", "PkgName": "zlib", "InstalledVersion": "1.2", "Severity": "HIGH"},
            {"VulnerabilityID": "CVE-2026-0003", "PkgName": "curl", "InstalledVersion": "8", "Severity": "weird"},
        ]},
        # The same finding in a second target counts once.
        {"Target": "copy", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2026-0001", "PkgName": "openssl", "InstalledVersion": "3.0.1",
             "Severity": "CRITICAL"}]},
    ],
}


class ScanCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.svc.scans.poll = 0.01
        self.engine.add_image(trivy.trivy_image())
        self.image = self.engine.add_image("nginx:1.27")
        self.engine.add_container("web", "nginx:1.27")
        self.exit_code, self.output = 0, ["2026-10-26T10:00:00.000000000Z " + json.dumps(REPORT)]
        self.engine.runners.append(("aquasec/trivy", lambda c: (self.exit_code, self.output)))


class TestScanApi(ScanCase):
    async def test_scan_wait_then_cached(self):
        await self.roles("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"wait": True})
        _, res = await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"wait": True}, expect=200)
        rec = res["result"]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual((rec["counts"]["critical"], rec["counts"]["high"], rec["counts"]["unknown"]), (1, 1, 1))
        self.assertEqual(rec["total"], 3)
        self.assertEqual(rec["fixable"], 1)
        self.assertEqual(rec["findings"][0]["id"], "CVE-2026-0001")
        # The scanner container is removed afterwards.
        self.assertFalse([c for c in self.engine.containers.values() if c["Config"]["Image"].startswith("aquasec")])

        _, got = await self.call("GET", f"{H}/images/nginx:1.27/scan", "viewer", expect=200)
        self.assertTrue(got["fresh"])
        self.assertEqual(got["scanner"]["version"], "0.75.0")
        # A fresh result is served without starting another scan.
        _, again = await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={}, expect=200)
        self.assertFalse(again["running"])

        _, host = await self.call("GET", f"{H}/scans", "viewer", expect=200)
        self.assertEqual(host["scanned"], 1)
        self.assertEqual(host["totals"]["critical"], 1)
        self.assertIn(rec["imageId"], host["containers"].values())
        _, summary = await self.call("GET", "/scans/summary", "viewer", expect=200)
        self.assertEqual(summary[0]["hostId"], "local")
        self.assertEqual(summary[0]["counts"]["critical"], 1)

    async def test_background_scan(self):
        status, res = await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"force": True})
        self.assertEqual(status, 202, res)
        for _ in range(200):
            _, got = await self.call("GET", f"{H}/images/nginx:1.27/scan", "viewer")
            if got["result"] and not got["running"]:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(got["result"]["status"], "ok")

    async def test_failing_scanner_is_recorded(self):
        self.exit_code, self.output = 1, ["FATAL db download failed"]
        status, res = await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"wait": True})
        self.assertEqual(status, 502)
        self.assertIn("db download failed", res["error"])
        _, got = await self.call("GET", f"{H}/images/nginx:1.27/scan", "viewer")
        self.assertEqual(got["result"]["status"], "error")

    async def test_scanner_without_json(self):
        self.output = ["no report here"]
        status, res = await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"wait": True})
        self.assertEqual(status, 502)
        self.assertIn("no JSON report", res["error"])

    async def test_scanner_image_pulled_when_missing(self):
        del self.engine.images[next(k for k, v in self.engine.images.items()
                                    if trivy.trivy_image() in v.get("RepoTags", []))]
        await self.call("POST", f"{H}/images/nginx:1.27/scan", "operator", json={"wait": True}, expect=200)
        self.assertTrue(any("images/create" in c[1] for c in self.engine.calls))

    async def test_unknown_image_and_host(self):
        await self.call("GET", f"{H}/images/missing:1/scan", "viewer", expect=404)
        await self.call("GET", "/hosts/nope/images/x/scan", "viewer", expect=404)


class TestTrivyParsing(unittest.TestCase):
    def test_parse_and_extract(self):
        doc = trivy.extract_json(b"noise\n2026-01-01T00:00:00.1Z " + json.dumps(REPORT).encode() + b"\ntrailer")
        s = trivy.parse_trivy_report(doc, max_findings=2)
        self.assertEqual(s.os, "debian 12.8")
        self.assertEqual((s.critical, s.high, s.total, s.fixable), (1, 1, 3, 1))
        self.assertEqual(len(s.findings), 2)
        self.assertEqual(trivy.sum_counts([s.counts, s.counts, None])["critical"], 2)
        for bad in (b"", b"{not json}", b"[1]"):
            with self.assertRaises(ValueError):
                trivy.extract_json(bad)
        self.assertEqual(trivy.scanner_version_from_image("aquasec/trivy:0.75.0"), "0.75.0")
        self.assertIn("--format", " ".join(trivy.trivy_command("nginx")))


if __name__ == "__main__":
    unittest.main()
