"""Image scanning: Trivy report parsing, log decoding, gate decisions, and the
ScanService run/re-use path against a fake Ops (no docker, no network).

The fixture ``tests/fixtures/trivy_report.json`` is a trimmed real report from
``aquasec/trivy:0.75.0`` on ``debian:bullseye-20240701-slim`` (20 OS findings,
plus a synthetic jar target that repeats one finding and adds a GHSA one).
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kowan.scan import gate, trivy
from kowan.scan.service import ScanService, is_fresh, pick_target
from kowan.store import Store

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "trivy_report.json")


def _fixture():
    with open(FIXTURE) as f:
        return json.load(f)


class TestParse(unittest.TestCase):
    def test_counts_dedupe_and_order(self):
        s = trivy.parse_trivy_report(_fixture())
        self.assertEqual(s.counts, {"critical": 3, "high": 6, "medium": 6, "low": 4, "unknown": 2})
        self.assertEqual(s.total, 21)  # the repeated finding in the jar target counted once
        self.assertEqual(s.scanner_version, "0.75.0")
        self.assertEqual(s.os, "debian 11.10")
        self.assertTrue(s.image_id.startswith("sha256:"))
        sev = [f["severity"] for f in s.findings]
        order = {k: i for i, k in enumerate(trivy.SEVERITIES)}
        self.assertEqual(sev, sorted(sev, key=order.__getitem__))
        first = s.findings[0]
        self.assertEqual(set(first), {"id", "pkg", "installed", "fixed", "severity", "title", "url", "target"})
        fixable = sum(1 for f in s.findings if f["fixed"])
        self.assertEqual(s.fixable, fixable)

    def test_cap_findings(self):
        s = trivy.parse_trivy_report(_fixture(), max_findings=5)
        self.assertEqual(len(s.findings), 5)
        self.assertEqual(s.total, 21)
        self.assertTrue(all(f["severity"] == "CRITICAL" for f in s.findings[:3]))

    def test_empty_report(self):
        s = trivy.parse_trivy_report({"ArtifactName": "x", "Results": [{"Target": "x", "Class": "os-pkgs"}]})
        self.assertEqual(s.total, 0)
        self.assertEqual(s.counts["critical"], 0)

    def test_strip_timestamps_and_extract(self):
        body = json.dumps(_fixture(), indent=2).encode()
        logged = b"\n".join(b"2026-10-25T10:00:00.123456789Z " + line for line in body.split(b"\n"))
        doc = trivy.extract_json(logged)
        self.assertEqual(doc["Trivy"]["Version"], "0.75.0")

    def test_extract_without_json(self):
        with self.assertRaises(ValueError):
            trivy.extract_json(b"2026-10-25T10:00:00Z FATAL unable to find the image\n")

    def test_spec(self):
        spec = trivy.build_scan_spec("nginx:latest", image="aquasec/trivy:0.75.0", volume="kowan-trivy-cache")
        self.assertEqual(spec.image, "aquasec/trivy:0.75.0")
        self.assertEqual(spec.command[:5], ["image", "--format", "json", "--scanners", "vuln"])
        self.assertEqual(spec.command[-1], "nginx:latest")
        sock = [v for v in spec.volumes if v.target == "/var/run/docker.sock"][0]
        self.assertTrue(sock.read_only)
        self.assertEqual([v.source for v in spec.volumes if v.target == "/cache"], ["kowan-trivy-cache"])
        self.assertEqual(spec.labels.get("kowan.support"), "true")
        self.assertEqual(trivy.scanner_version_from_image("aquasec/trivy:0.75.0"), "0.75.0")


class TestGate(unittest.TestCase):
    C = {"critical": 1, "high": 0}
    H = {"critical": 0, "high": 3}
    CLEAN = {"critical": 0, "high": 0}

    def test_never(self):
        self.assertEqual(gate.gate_decision("never", self.C), (False, ""))
        self.assertFalse(gate.gate_decision("never", None)[0])

    def test_critical(self):
        self.assertTrue(gate.gate_decision("critical", self.C)[0])
        self.assertFalse(gate.gate_decision("critical", self.H)[0])

    def test_high(self):
        self.assertTrue(gate.gate_decision("high", self.H)[0])
        self.assertTrue(gate.gate_decision("high", self.C)[0])
        self.assertFalse(gate.gate_decision("high", self.CLEAN)[0])

    def test_worse(self):
        self.assertTrue(gate.gate_decision("worse", {"critical": 1, "high": 2}, {"critical": 0, "high": 5})[0])
        self.assertTrue(gate.gate_decision("worse", {"critical": 0, "high": 6}, {"critical": 0, "high": 5})[0])
        self.assertFalse(gate.gate_decision("worse", {"critical": 2, "high": 5}, {"critical": 2, "high": 5})[0])
        self.assertFalse(gate.gate_decision("worse", {"critical": 0, "high": 1}, {"critical": 3, "high": 9})[0])
        # unknown baseline: only critical blocks
        self.assertFalse(gate.gate_decision("worse", self.H, None)[0])
        self.assertTrue(gate.gate_decision("worse", self.C, None)[0])

    def test_fail_closed(self):
        blocked, reason = gate.gate_decision("critical", None)
        self.assertTrue(blocked)
        self.assertIn("could not be scanned", reason)

    def test_parse(self):
        self.assertEqual(gate.parse_block_policy("Critical"), "critical")
        self.assertEqual(gate.parse_block_policy(""), "never")
        self.assertEqual(gate.parse_block_policy("worse"), "worse")
        with self.assertRaises(ValueError):
            gate.parse_block_policy("sometimes")


class _Stream:
    def __init__(self, data):
        self._chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        pass


class FakeOps:
    def __init__(self, exit_code=0, output=None):
        self.exit_code = exit_code
        self.output = output if output is not None else json.dumps(_fixture(), indent=1).encode()
        self.created = []
        self.removed = []
        self.pulled = []
        self.polls = 0
        self.images = {
            "kowan-f1-debian:old": {"Id": "sha256:abc", "RepoTags": ["kowan-f1-debian:old"], "RepoDigests": []},
            "sha256:abc": {"Id": "sha256:abc", "RepoTags": ["kowan-f1-debian:old"], "RepoDigests": []},
        }

    async def inspect_image(self, ref):
        if ref.startswith("aquasec/trivy") and not self.pulled:
            raise RuntimeError("no such image")
        if ref.startswith("aquasec/trivy"):
            return {"Id": "sha256:trivy"}
        return self.images[ref]

    async def pull_image(self, ref, auth):
        self.pulled.append(ref)
        return _Stream(b'{"status":"done"}\n')

    async def create_container(self, name, spec, start):
        self.created.append((name, spec, start))
        return "scan-cid"

    async def inspect_container(self, cid):
        self.polls += 1
        if self.polls < 2:
            return {"State": {"Running": True, "Status": "running"}}
        return {"State": {"Running": False, "Status": "exited", "ExitCode": self.exit_code}}

    async def container_logs(self, cid, follow, tail):
        lines = self.output.split(b"\n")
        return _Stream(b"\n".join(b"2026-10-25T10:00:00.000000001Z " + l for l in lines))

    async def remove_container(self, cid, force):
        self.removed.append(cid)


class _Svc:
    def __init__(self, store):
        self.store = store


async def _nosleep(_):
    return None


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        os.environ["KOWAN_TRIVY_CONTAINER_PREFIX"] = "kowan-f1-trivy-"

    def tearDown(self):
        os.environ.pop("KOWAN_TRIVY_CONTAINER_PREFIX", None)
        self.store.close()
        self.tmp.cleanup()

    def test_scan_then_reuse(self):
        ops = FakeOps()
        svc = ScanService(_Svc(self.store), sleep=_nosleep)
        rec = asyncio.run(svc.scan("h1", "kowan-f1-debian:old", ops=ops))
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["imageId"], "sha256:abc")
        self.assertEqual(rec["counts"]["critical"], 3)
        self.assertEqual(rec["scannerVersion"], "0.75.0")
        self.assertEqual(len(rec["findings"]), 21)
        self.assertEqual(ops.removed, ["scan-cid"])
        self.assertTrue(ops.created[0][0].startswith("kowan-f1-trivy-"))
        self.assertEqual(ops.created[0][1].command[-1], "kowan-f1-debian:old")
        self.assertEqual(ops.pulled, ["aquasec/trivy:0.75.0"])
        # second call re-uses the fresh result: no new container
        rec2 = asyncio.run(svc.scan("h1", "sha256:abc", ops=ops))
        self.assertEqual(rec2["scannedAt"], rec["scannedAt"])
        self.assertEqual(len(ops.created), 1)
        # force re-scans
        ops.polls = 0
        asyncio.run(svc.scan("h1", "sha256:abc", ops=ops, force=True))
        self.assertEqual(len(ops.created), 2)

    def test_failure_recorded_and_raised(self):
        ops = FakeOps(exit_code=1, output=b"FATAL image scan error")
        svc = ScanService(_Svc(self.store), sleep=_nosleep)
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(svc.scan("h1", "kowan-f1-debian:old", ops=ops))
        self.assertIn("exited with code 1", str(ctx.exception))
        rec = self.store.get_image_scan("sha256:abc")
        self.assertEqual(rec["status"], "error")
        self.assertEqual(ops.removed, ["scan-cid"])

    def test_failed_rescan_keeps_good_result(self):
        svc = ScanService(_Svc(self.store), sleep=_nosleep)
        asyncio.run(svc.scan("h1", "sha256:abc", ops=FakeOps()))
        with self.assertRaises(RuntimeError):
            asyncio.run(svc.scan("h1", "sha256:abc", ops=FakeOps(exit_code=2, output=b"boom"), force=True))
        self.assertEqual(self.store.get_image_scan("sha256:abc")["status"], "ok")

    def test_freshness(self):
        now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        rec = {"status": "ok", "scannedAt": "2026-10-25T00:00:01Z"}
        self.assertTrue(is_fresh(rec, now))
        self.assertFalse(is_fresh(rec, now + timedelta(hours=13)))
        self.assertFalse(is_fresh({"status": "error", "scannedAt": "2026-10-25T11:00:00Z"}, now))
        self.assertFalse(is_fresh(None, now))

    def test_pick_target(self):
        self.assertEqual(pick_target({"Id": "sha256:x", "RepoTags": ["a:1", "b:2"]}, "b:2"), "b:2")
        self.assertEqual(pick_target({"Id": "sha256:x", "RepoTags": ["a:1"]}, "sha256:x"), "a:1")
        self.assertEqual(pick_target({"Id": "sha256:x", "RepoTags": ["<none>:<none>"]}, "sha256:x"), "sha256:x")


if __name__ == "__main__":
    unittest.main()
