"""Tests for the built-in image update detection (``kowan/api/updates.py``).

Fast stdlib unittest, NO network / docker: covers the pure helpers and the
per-host report against a fake Ops whose registry lookup is stubbed.

  * ``parse_image_ref``  — docker normalisation (Hub library/, host:port, tags,
                           digests, image ids rejected);
  * ``compare_digests``  — canonical repo matching, multi-digest images, unknown
                           digests never flag an update;
  * ``DigestCache``      — TTL expiry, shorter error TTL, refresh bypass and
                           in-flight de-duplication;
  * ``check_host``       — digest-pinned / local-only / registry-error rows
                           report ``updateAvailable=False`` with an error.
"""

import asyncio
import unittest
from unittest import mock

from kowan.api import updates
from kowan.api.updates import DigestCache, compare_digests, parse_image_ref
from kowan.models import Container


class TestParseImageRef(unittest.TestCase):
    def test_docker_hub_official(self):
        r = parse_image_ref("nginx")
        self.assertEqual((r.registry, r.repository, r.tag, r.digest), ("docker.io", "library/nginx", "latest", ""))
        self.assertEqual(r.api_host, "registry-1.docker.io")
        self.assertEqual(r.pull_ref, "nginx:latest")
        self.assertEqual(r.key, "docker.io/library/nginx:latest")

    def test_docker_hub_user_repo_with_tag(self):
        r = parse_image_ref("myorg/app:1.2")
        self.assertEqual((r.registry, r.repository, r.tag), ("docker.io", "myorg/app", "1.2"))
        self.assertEqual(r.pull_ref, "myorg/app:1.2")

    def test_explicit_hub_hosts_normalise(self):
        for ref in ("docker.io/library/nginx:1", "index.docker.io/nginx:1", "docker.io/nginx:1"):
            self.assertEqual(parse_image_ref(ref).key, "docker.io/library/nginx:1", ref)

    def test_private_registry_with_port(self):
        r = parse_image_ref("host:5000/team/x")
        self.assertEqual((r.registry, r.repository, r.tag), ("host:5000", "team/x", "latest"))
        self.assertEqual(r.api_host, "host:5000")
        self.assertEqual(r.pull_ref, "host:5000/team/x:latest")

    def test_ghcr_and_localhost(self):
        self.assertEqual(parse_image_ref("ghcr.io/o/a:v1").name, "ghcr.io/o/a")
        self.assertEqual(parse_image_ref("localhost/x:dev").registry, "localhost")

    def test_digest(self):
        r = parse_image_ref("nginx:1.25@sha256:abc")
        self.assertEqual((r.tag, r.digest), ("1.25", "sha256:abc"))
        self.assertEqual(parse_image_ref("nginx@sha256:abc").digest, "sha256:abc")

    def test_rejects_ids_and_empty(self):
        for bad in ("", "  ", "sha256:" + "a" * 64, "f" * 64):
            with self.assertRaises(ValueError, msg=bad):
                parse_image_ref(bad)


class TestCompareDigests(unittest.TestCase):
    def test_same_digest_no_update(self):
        ref = parse_image_ref("nginx:latest")
        local, avail = compare_digests(["nginx@sha256:aaa"], ref, "sha256:aaa")
        self.assertEqual((local, avail), ("sha256:aaa", False))

    def test_different_digest_update(self):
        ref = parse_image_ref("nginx")
        local, avail = compare_digests(["docker.io/library/nginx@sha256:aaa"], ref, "sha256:bbb")
        self.assertEqual((local, avail), ("sha256:aaa", True))

    def test_prefers_matching_repo_and_accepts_any_of_its_digests(self):
        ref = parse_image_ref("ghcr.io/o/a:v1")
        digests = ["other/x@sha256:zzz", "ghcr.io/o/a@sha256:old", "ghcr.io/o/a@sha256:new"]
        local, avail = compare_digests(digests, ref, "sha256:new")
        self.assertEqual((local, avail), ("sha256:old", False))

    def test_unknown_digests_never_flag(self):
        ref = parse_image_ref("nginx")
        self.assertEqual(compare_digests([], ref, "sha256:x"), ("", False))
        self.assertEqual(compare_digests(["nginx@sha256:a"], ref, ""), ("sha256:a", False))
        self.assertEqual(compare_digests(["garbage"], ref, "sha256:x"), ("", False))


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestDigestCache(unittest.TestCase):
    def test_ttl_and_refresh(self):
        clock = _Clock()
        cache = DigestCache(ttl=600, error_ttl=60, clock=clock)
        calls = []

        async def fetch():
            calls.append(1)
            return f"sha256:{len(calls)}"

        async def run():
            e1 = await cache.resolve("k", fetch)
            clock.t += 300
            e2 = await cache.resolve("k", fetch)
            e3 = await cache.resolve("k", fetch, refresh=True)
            clock.t += 601
            e4 = await cache.resolve("k", fetch)
            return e1, e2, e3, e4

        e1, e2, e3, e4 = asyncio.run(run())
        self.assertEqual(e1.digest, "sha256:1")
        self.assertEqual(e2.digest, "sha256:1")  # cached
        self.assertEqual(e3.digest, "sha256:2")  # refresh bypasses
        self.assertEqual(e4.digest, "sha256:3")  # expired
        self.assertEqual(len(calls), 3)

    def test_errors_cached_with_shorter_ttl(self):
        clock = _Clock()
        cache = DigestCache(ttl=600, error_ttl=60, clock=clock)
        calls = []

        async def boom():
            calls.append(1)
            raise ValueError("registry down")

        async def run():
            e1 = await cache.resolve("k", boom)
            clock.t += 30
            await cache.resolve("k", boom)
            clock.t += 31
            await cache.resolve("k", boom)
            return e1

        e1 = asyncio.run(run())
        self.assertEqual((e1.digest, e1.error), ("", "registry down"))
        self.assertEqual(len(calls), 2)

    def test_put_drops_expired_entries(self):
        clock = _Clock()
        cache = DigestCache(ttl=600, error_ttl=60, clock=clock)
        cache.put("old", "sha256:a")
        cache.put("failed", "", "registry down")
        clock.t += 61
        cache.put("new", "sha256:b")
        self.assertEqual(sorted(cache._entries), ["new", "old"])  # the error entry expired
        clock.t += 540
        cache.put("newer", "sha256:c")
        self.assertEqual(sorted(cache._entries), ["new", "newer"])

    def test_concurrent_lookups_share_one_fetch(self):
        cache = DigestCache()
        calls = []

        async def slow():
            calls.append(1)
            await asyncio.sleep(0.01)
            return "sha256:x"

        async def run():
            return await asyncio.gather(*(cache.resolve("k", slow) for _ in range(5)))

        entries = asyncio.run(run())
        self.assertEqual({e.digest for e in entries}, {"sha256:x"})
        self.assertEqual(len(calls), 1)


class _FakeOps:
    def __init__(self, containers, images):
        self._containers = containers
        self._images = images  # image id -> inspect doc

    async def list_containers(self, all):
        return self._containers

    async def inspect_container(self, cid):
        return {"Image": "img-" + cid}

    async def inspect_image(self, image_id):
        return self._images.get(image_id, {})


class TestCheckHost(unittest.TestCase):
    def test_rows(self):
        containers = [
            Container(id="a", names=["/web"], image="nginx:latest"),
            Container(id="b", names=["/pinned"], image="nginx@sha256:pin"),
            Container(id="c", names=["/built"], image="myapp:dev"),
            Container(id="d", names=["/priv"], image="reg.example.com/x:1"),
            Container(id="e", names=["/byid"], image="sha256:" + "e" * 64),
        ]
        images = {
            "img-a": {"RepoDigests": ["nginx@sha256:old"]},
            "img-c": {"RepoDigests": []},
            "img-d": {"RepoDigests": ["reg.example.com/x@sha256:same"]},
        }

        async def fake_remote(store, ref, plain_http=False):
            if ref.registry == "reg.example.com":
                raise ValueError("401 unauthorized")
            return "sha256:new"

        async def run():
            with mock.patch.object(updates, "_remote_digest", fake_remote):
                return await updates.check_host(
                    mock.Mock(store=None), _FakeOps(containers, images), cache=DigestCache()
                )

        rows = {r["containerName"]: r for r in asyncio.run(run())}
        self.assertTrue(rows["web"]["updateAvailable"])
        self.assertEqual(rows["web"]["localDigest"], "sha256:old")
        self.assertEqual(rows["web"]["remoteDigest"], "sha256:new")
        self.assertNotIn("error", rows["web"])
        for name in ("pinned", "built", "priv", "byid"):
            self.assertFalse(rows[name]["updateAvailable"], name)
            self.assertTrue(rows[name].get("error"), name)
        self.assertIn("401", rows["priv"]["error"])
        self.assertEqual(rows["pinned"]["localDigest"], "sha256:pin")
        for r in rows.values():
            self.assertTrue(r["checkedAt"])


class TestCheckImage(unittest.TestCase):
    """One image, every tag, asked of the registry now (cache bypassed)."""

    def test_rows_per_tag(self):
        images = {
            "sha256:app": {"RepoTags": ["nginx:latest", "nginx:1.27", "<none>:<none>"], "RepoDigests": ["nginx@sha256:old"]},
            "sha256:local": {"RepoTags": ["myapp:dev"], "RepoDigests": []},
            "sha256:bare": {"RepoTags": [], "RepoDigests": []},
        }
        seen = []

        async def fake_remote(store, ref, plain_http=False):
            seen.append(ref.tag)
            if ref.tag == "1.27":
                raise ValueError("401 unauthorized")
            return "sha256:new"

        async def run(image_id, cache):
            with mock.patch.object(updates, "_remote_digest", fake_remote):
                return await updates.check_image(mock.Mock(store=None), _FakeOps([], images), image_id, cache=cache)

        cache = DigestCache()
        rows = {r["tag"]: r for r in asyncio.run(run("sha256:app", cache))}
        self.assertEqual(set(rows), {"nginx:latest", "nginx:1.27"})
        self.assertTrue(rows["nginx:latest"]["updateAvailable"])
        self.assertEqual((rows["nginx:latest"]["localDigest"], rows["nginx:latest"]["remoteDigest"]), ("sha256:old", "sha256:new"))
        self.assertFalse(rows["nginx:1.27"]["updateAvailable"])
        self.assertIn("401", rows["nginx:1.27"]["error"])
        # A second check asks again: the cache is bypassed.
        asyncio.run(run("sha256:app", cache))
        self.assertEqual(seen.count("latest"), 2)
        [local] = asyncio.run(run("sha256:local", cache))
        self.assertEqual(local["error"], "local image (no registry digest)")
        self.assertEqual(asyncio.run(run("sha256:bare", cache)), [])
        [bad] = asyncio.run(run_bad_ref())
        self.assertTrue(bad["error"])


async def run_bad_ref():
    images = {"x": {"RepoTags": ["sha256:" + "a" * 64], "RepoDigests": ["a@sha256:b"]}}
    return await updates.check_image(mock.Mock(store=None), _FakeOps([], images), "x", cache=DigestCache())


class TestContainerPortsOrder(unittest.TestCase):
    def test_ports_sorted_whatever_docker_returns(self):
        raw = [{"privatePort": 443, "publicPort": 8443, "type": "tcp", "ip": "::"},
               {"privatePort": 80, "publicPort": 8080, "type": "udp", "ip": "0.0.0.0"},
               {"privatePort": 80, "publicPort": 8080, "type": "tcp", "ip": "::"},
               {"privatePort": 80, "publicPort": 8080, "type": "tcp", "ip": "0.0.0.0"}]
        c = Container.model_validate({"id": "a", "ports": raw})
        self.assertEqual([(p.private_port, p.type, p.ip) for p in c.ports],
                         [(80, "tcp", "0.0.0.0"), (80, "tcp", "::"), (80, "udp", "0.0.0.0"), (443, "tcp", "::")])


class _FakeRemoteOps:
    """Remote-style Ops (no clone_recreate): spec round-trip recreate."""

    def __init__(self, fail_first_create=False):
        self.calls = []
        self.fail_first_create = fail_first_create

    async def container_spec(self, cid):
        from kowan.models import ContainerSpec, KeyValue

        return ContainerSpec(
            image="nginx:latest",
            env=[KeyValue(key="PATH", value="/usr/bin"), KeyValue(key="MINE", value="1")],
            labels={"maintainer": "nginx", "app": "web"},
        )

    async def inspect_image(self, image_id):
        return {"Config": {"Env": ["PATH=/usr/bin"], "Labels": {"maintainer": "nginx"}}}

    async def stop_container(self, cid):
        self.calls.append(("stop", cid))

    async def remove_container(self, cid, force):
        self.calls.append(("remove", cid))

    async def create_container(self, name, spec, start):
        self.calls.append(("create", name, spec.image, start, spec))
        if self.fail_first_create and len([c for c in self.calls if c[0] == "create"]) == 1:
            raise RuntimeError("port already allocated")
        return "new-id"


class TestRecreateRemote(unittest.TestCase):
    INSP = {
        "Name": "/web",
        "Image": "sha256:oldimage",
        "State": {"Running": True},
        "Config": {"Labels": {"com.docker.compose.project": "stack", "app": "web", "maintainer": "nginx"}},
    }

    def test_spec_roundtrip_inherits_new_image_defaults(self):
        ops = _FakeRemoteOps()
        new_id = asyncio.run(updates.recreate_with_image(ops, "old", self.INSP, "nginx:latest"))
        self.assertEqual(new_id, "new-id")
        self.assertEqual([c[0] for c in ops.calls], ["stop", "remove", "create"])
        _, name, image, start, spec = ops.calls[-1]
        self.assertEqual((name, image, start), ("web", "nginx:latest", True))
        self.assertEqual([kv.key for kv in spec.env], ["MINE"])
        self.assertEqual(spec.labels, {"app": "web", "com.docker.compose.project": "stack"})

    def test_failed_create_restores_old_image(self):
        ops = _FakeRemoteOps(fail_first_create=True)
        with self.assertRaises(updates.BadGateway) as ctx:
            asyncio.run(updates.recreate_with_image(ops, "old", self.INSP, "nginx:latest"))
        self.assertIn("previous container restored", str(ctx.exception))
        creates = [c for c in ops.calls if c[0] == "create"]
        self.assertEqual(creates[-1][2], "sha256:oldimage")


if __name__ == "__main__":
    unittest.main()
