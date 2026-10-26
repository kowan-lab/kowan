"""Polling triggers over HTTP, with httpx's MockTransport standing in for the
registry and the git host: the docker-image Bearer handshake and digest
fallback, registry_v2_get, the git smart-HTTP ref advertisement, and the
workflow poller seeding, firing a run, and recording errors."""

import asyncio
import base64
import hashlib
import json
import unittest
from unittest import mock

import httpx

from kowan import triggers
from kowan.triggers import dockerimage, gitcommit
from tests.apicase import ServerCase

_RealClient = httpx.AsyncClient


def _patch_transport(handler):
    def factory(*args, **kw):
        kw.pop("transport", None)
        return _RealClient(*args, transport=httpx.MockTransport(handler), **kw)
    return mock.patch.object(httpx, "AsyncClient", factory)


class FakeRegistryHost:
    """registry.test: Bearer challenge -> token (Basic) -> manifest with digest."""

    def __init__(self):
        self.digest = "sha256:" + "a" * 64
        self.header = True
        self.manifest = b'{"schemaVersion": 2}'
        self.token_requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/token":
            self.token_requests.append(request)
            if request.headers.get("Authorization") != "Basic " + base64.b64encode(b"bob:pw").decode():
                return httpx.Response(401, text="bad credentials")
            return httpx.Response(200, json={"token": "T"})
        if request.headers.get("Authorization") != "Bearer T":
            return httpx.Response(401, headers={"WWW-Authenticate":
                'Bearer realm="https://registry.test/token",service="registry.test",scope="repository:app:pull,push"'})
        if url.path == "/v2/app/manifests/1.0":
            headers = {"Docker-Content-Digest": self.digest} if self.header else {}
            return httpx.Response(200, headers=headers, content=b"" if request.method == "HEAD" else self.manifest)
        if url.path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["app"]})
        return httpx.Response(404, text="nope")


def _pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


class TestDockerImage(unittest.IsolatedAsyncioTestCase):
    def poller(self, **cfg):
        base = {"registry": "registry.test", "repository": "app", "tag": "1.0", "username": "bob", "password": "pw"}
        return triggers.build(triggers.TypeDockerImage, {**base, **cfg})

    async def test_seed_then_fire_on_new_digest(self):
        reg = FakeRegistryHost()
        with _patch_transport(reg):
            p = self.poller()
            fired, state = await p.evaluate({}, "")
            self.assertEqual((fired, state), (False, reg.digest))
            self.assertEqual(dict(reg.token_requests[0].url.params)["scope"], "repository:app:pull,push")
            self.assertEqual(await p.evaluate({}, state), (False, reg.digest))
            reg.digest = "sha256:" + "b" * 64
            self.assertEqual(await p.evaluate({}, state), (True, reg.digest))
            # No digest header: the manifest body is hashed.
            reg.header = False
            fired, state = await p.evaluate({}, state)
            self.assertEqual(state, "sha256:" + hashlib.sha256(reg.manifest).hexdigest())
            # registry_v2_get shares the handshake.
            got = await dockerimage.registry_v2_get("registry.test", "/v2/_catalog", "bob", "pw")
            self.assertEqual(got, {"repositories": ["app"]})
            with self.assertRaises(ValueError):
                await dockerimage.registry_v2_get("registry.test", "/v2/missing", "bob", "pw")

    async def test_errors(self):
        reg = FakeRegistryHost()
        with _patch_transport(reg):
            with self.assertRaises(ValueError) as cm:
                await self.poller(password="wrong").evaluate({}, "")
            self.assertIn("token", str(cm.exception))
            with self.assertRaises(ValueError):
                await self.poller(tag="2.0").evaluate({}, "")
        with _patch_transport(lambda r: httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="x"'})):
            with self.assertRaises(ValueError):
                await self.poller().evaluate({}, "")
        with _patch_transport(lambda r: httpx.Response(401, headers={"WWW-Authenticate": "Bearer service=x"})):
            with self.assertRaises(ValueError):
                await self.poller().evaluate({}, "")

        def bad_token(r):
            if r.url.path == "/token":
                return httpx.Response(200, text="not json")
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://registry.test/token"'})

        with _patch_transport(bad_token):
            with self.assertRaises(ValueError):
                await self.poller().evaluate({}, "")

    def test_config_and_challenge(self):
        p = triggers.build(triggers.TypeDockerImage, {"repository": "nginx"})
        self.assertEqual((p.registry, p.repository, p.reference), ("registry-1.docker.io", "library/nginx", "latest"))
        with self.assertRaises(ValueError):
            triggers.build(triggers.TypeDockerImage, {})
        self.assertEqual(dockerimage._parse_challenge('realm="r",scope="a:b,c"'), {"realm": "r", "scope": "a:b,c"})


class TestGitCommit(unittest.IsolatedAsyncioTestCase):
    def adv(self, refs):
        body = _pkt("# service=git-upload-pack\n") + b"0000"
        first = True
        for name, sha in refs.items():
            body += _pkt(f"{sha} {name}" + ("\x00multi_ack side-band-64k" if first else "") + "\n")
            first = False
        return body + b"0000"

    async def test_branches(self):
        refs = {"refs/heads/main": "1" * 40, "refs/heads/dev": "2" * 40, "refs/tags/v1": "3" * 40}

        def host(request):
            self.assertEqual(request.url.params["service"], "git-upload-pack")
            return httpx.Response(200, content=self.adv(refs))

        with _patch_transport(host):
            p = triggers.build(triggers.TypeGitCommit, {"repo": "https://git.test/a/b.git", "token": "t"})
            self.assertEqual(await p.evaluate({}, ""), (False, "1" * 40))
            refs["refs/heads/main"] = "4" * 40
            self.assertEqual(await p.evaluate({}, "1" * 40), (True, "4" * 40))
            p = triggers.build(triggers.TypeGitCommit, {"repo": "https://git.test/a/b.git", "branch": "dev"})
            self.assertEqual((await p.evaluate({}, ""))[1], "2" * 40)
            with self.assertRaises(ValueError):
                await triggers.build(triggers.TypeGitCommit, {"repo": "https://git.test/x", "branch": "nope"}).evaluate({}, "")
        # master fallback, errors.
        with _patch_transport(lambda r: httpx.Response(200, content=self.adv({"refs/heads/master": "5" * 40}))):
            p = triggers.build(triggers.TypeGitCommit, {"repo": "https://git.test/old"})
            self.assertEqual((await p.evaluate({}, ""))[1], "5" * 40)
        with _patch_transport(lambda r: httpx.Response(403, text="forbidden")):
            with self.assertRaises(ValueError):
                await p.evaluate({}, "")
        with _patch_transport(lambda r: httpx.Response(200, content=b"zzzzjunk")):
            with self.assertRaises(ValueError):
                await p.evaluate({}, "")
        with self.assertRaises(ValueError):
            triggers.build(triggers.TypeGitCommit, {})
        self.assertFalse(gitcommit._is_hex40("x" * 40))


class TestWorkflowPoller(ServerCase):
    async def test_seed_fire_and_error(self):
        reg = FakeRegistryHost()
        _, wf = await self.call("POST", "/workflows", "admin", json={
            "name": "on-push", "enabled": True,
            "steps": [{"name": "b", "type": "backup", "config": {}}],
            "triggers": [{"type": "docker-image", "enabled": True,
                          "config": {"repository": "app", "tag": "1.0"}}]}, expect=201)
        poller = self.svc.workflow_poller
        # Anonymous against registry.test -> the default registry is Docker Hub; point it at ours.
        stored = self.svc.store.get_workflow(wf["id"])
        stored.triggers[0].config["registry"] = "registry.test"
        self.svc.store.update_workflow(stored)
        with _patch_transport(reg):
            await poller.poll_once()  # anonymous: the token endpoint refuses -> error recorded
            t = self.svc.store.get_workflow(wf["id"]).triggers[0]
            self.assertIn("token", t.last_error)
            # Give it credentials through [[secret]] references.
            await self.call("POST", "/credentials", "admin", json={
                "kind": "secret", "name": "reg-pass", "fields": {"value": "pw"}}, expect=201)
            stored = self.svc.store.get_workflow(wf["id"])
            stored.triggers[0].config.update(username="bob", password="[[reg-pass]]")
            self.svc.store.update_workflow(stored)
            await poller.poll_once()  # seeds
            t = self.svc.store.get_workflow(wf["id"]).triggers[0]
            self.assertEqual((t.state, t.last_error), (reg.digest, ""))
            reg.digest = "sha256:" + "c" * 64
            await poller.poll_once()  # fires
            t = self.svc.store.get_workflow(wf["id"]).triggers[0]
            self.assertEqual(t.state, reg.digest)
            self.assertTrue(t.last_fired)
        for _ in range(100):
            _, runs = await self.call("GET", f"/workflows/{wf['id']}/runs", "operator", expect=200)
            if runs:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(runs[0]["trigger"], "docker-image")


if __name__ == "__main__":
    unittest.main()


class TestPollerErrorPaths(ServerCase):
    async def test_connection_build_and_run_failures(self):
        from unittest import mock as _mock

        steps = [{"name": "b", "type": "backup", "config": {}}]
        _, wf = await self.call("POST", "/workflows", "admin", json={
            "name": "p", "enabled": True, "steps": steps,
            "triggers": [{"type": "git-commit", "enabled": True,
                          "config": {"repo": "https://git.test/a.git", "connectionRef": "missing"}}]}, expect=201)
        poller = self.svc.workflow_poller
        await poller.poll_once()
        t = self.svc.store.get_workflow(wf["id"]).triggers[0]
        self.assertTrue(t.last_error)
        await poller.poll_once()  # same error: nothing new to save
        # A git connection supplying the token for a private repository.
        _, conn = await self.call("POST", "/credentials", "admin", json={
            "kind": "connection", "type": "github", "name": "gh", "config": {"token": "ghp_x"}}, expect=201)
        stored = self.svc.store.get_workflow(wf["id"])
        stored.triggers[0].config["connectionRef"] = conn["id"]
        self.svc.store.update_workflow(stored)
        seen = []

        def host(request):
            seen.append(request.headers.get("Authorization", ""))
            data = _pkt("# service=git-upload-pack\n") + b"0000" + _pkt(f"{'9' * 40} refs/heads/main\x00caps\n") + b"0000"
            return httpx.Response(200, content=data)

        with _patch_transport(host):
            await poller.poll_once()
        t = self.svc.store.get_workflow(wf["id"]).triggers[0]
        self.assertEqual((t.state, t.last_error), ("9" * 40, ""))
        self.assertTrue(seen and seen[0].startswith("Basic "))
        # The workflow run cannot start: the state is not advanced, the error kept.
        stored = self.svc.store.get_workflow(wf["id"])
        stored.triggers[0].state = "0" * 40
        self.svc.store.update_workflow(stored)
        with _patch_transport(host), _mock.patch.object(poller._engine, "run", side_effect=RuntimeError("busy")):
            await poller.poll_once()
        t = self.svc.store.get_workflow(wf["id"]).triggers[0]
        self.assertEqual(t.state, "0" * 40)
        self.assertIn("busy", t.last_error)
        with _mock.patch.object(self.svc.store, "list_workflows", side_effect=RuntimeError("db")):
            await poller.poll_once()
        # Deleted while polling: the save is skipped quietly.
        poller._save_trigger_state("gone", [])


class TestImageDigestHelpers(ServerCase):
    async def test_remote_digest_uses_stored_registry_credentials(self):
        from kowan import agentupdate

        reg = FakeRegistryHost()
        await self.call("POST", "/credentials", "admin", json={
            "kind": "registry", "name": "rt", "url": "registry.test", "username": "bob", "password": "pw"},
            expect=201)
        with _patch_transport(reg):
            self.assertEqual(await agentupdate._remote_digest(self.svc.store, "registry.test/app:1.0"), reg.digest)
        self.assertEqual(await agentupdate._registry_creds(self.svc.store, "unknown.example"), ("", ""))
