"""Git source for compose deployments: webhook verification, ref matching,
changed-path filtering, .env interpolation, the store round-trip and an offline
clone + deploy against a local repository (skipped when git is missing).
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from kowan.deployment import gitsource
from kowan.deployment.gitsource import (
    changed_paths,
    evaluate_push,
    interpolate,
    parse_env_file,
    paths_touch_dir,
    ref_matches,
    sign_body,
    verify_gitlab_token,
    verify_hub_signature,
)
from kowan.models import Deployment, GitSource
from kowan.store import Store

SECRET = "s3cret-value"


def _git(**kw) -> GitSource:
    base = dict(repoUrl="https://example.com/o/r.git", ref="main",
                path="stack/docker-compose.yml", autoDeploy=True, webhookSecret=SECRET)
    base.update(kw)
    return GitSource(**base)


def _push(ref="refs/heads/main", files=("stack/docker-compose.yml",)) -> bytes:
    return json.dumps({
        "ref": ref,
        "after": "a" * 40,
        "commits": [{"added": [], "modified": list(files), "removed": []}],
    }).encode()


class TestSignatures(unittest.TestCase):
    def test_hub_signature_roundtrip(self):
        body = b'{"ref":"refs/heads/main"}'
        sig = sign_body(SECRET, body)
        self.assertTrue(sig.startswith("sha256="))
        self.assertTrue(verify_hub_signature(SECRET, body, sig))
        # Gitea's bare-hex X-Gitea-Signature form.
        self.assertTrue(verify_hub_signature(SECRET, body, sig[7:]))
        self.assertTrue(verify_hub_signature(SECRET, body, sig.upper().replace("SHA256=", "sha256=")))

    def test_hub_signature_rejects(self):
        body = b"payload"
        sig = sign_body(SECRET, body)
        self.assertFalse(verify_hub_signature(SECRET, body + b"x", sig))
        self.assertFalse(verify_hub_signature("other", body, sig))
        self.assertFalse(verify_hub_signature(SECRET, body, ""))
        self.assertFalse(verify_hub_signature("", body, sig))
        self.assertFalse(verify_hub_signature(SECRET, body, "sha256=zz"))

    def test_gitlab_token(self):
        self.assertTrue(verify_gitlab_token(SECRET, SECRET))
        self.assertFalse(verify_gitlab_token(SECRET, "nope"))
        self.assertFalse(verify_gitlab_token("", ""))
        self.assertFalse(verify_gitlab_token(SECRET, ""))


class TestRefMatching(unittest.TestCase):
    def test_branch(self):
        self.assertTrue(ref_matches("main", "refs/heads/main"))
        self.assertTrue(ref_matches("", "refs/heads/main"))  # default branch
        self.assertFalse(ref_matches("main", "refs/heads/dev"))
        self.assertFalse(ref_matches("main", "refs/heads/main-2"))
        self.assertTrue(ref_matches("feature/x", "refs/heads/feature/x"))

    def test_tag_and_qualified(self):
        self.assertTrue(ref_matches("v1.2.0", "refs/tags/v1.2.0"))
        self.assertTrue(ref_matches("refs/heads/main", "refs/heads/main"))
        self.assertFalse(ref_matches("refs/heads/main", "refs/tags/main"))
        self.assertFalse(ref_matches("main", ""))
        self.assertTrue(ref_matches("main", "main"))


class TestChangedPaths(unittest.TestCase):
    def test_collects_files(self):
        payload = {"commits": [
            {"added": ["a"], "modified": ["b"], "removed": ["c"]},
            {"added": [], "modified": ["d/e"], "removed": []},
        ]}
        self.assertEqual(changed_paths(payload), ["a", "b", "c", "d/e"])

    def test_unknown_when_not_listed(self):
        self.assertIsNone(changed_paths({}))
        self.assertIsNone(changed_paths({"commits": []}))
        self.assertIsNone(changed_paths({"commits": [{"id": "x"}]}))

    def test_paths_touch_dir(self):
        self.assertTrue(paths_touch_dir(["stack/.env"], "stack/docker-compose.yml"))
        self.assertTrue(paths_touch_dir(["stack/sub/x"], "/stack/docker-compose.yml"))
        self.assertFalse(paths_touch_dir(["stacks/x", "README.md"], "stack/docker-compose.yml"))
        self.assertFalse(paths_touch_dir(["stack"], "stack/sub/compose.yml"))
        # Compose at the repo root: any change counts, none does not.
        self.assertTrue(paths_touch_dir(["anything/at/all"], "docker-compose.yml"))
        self.assertFalse(paths_touch_dir([], "docker-compose.yml"))


class TestEvaluatePush(unittest.TestCase):
    def _gh(self, body, event="push", secret=SECRET):
        return {"X-GitHub-Event": event, "X-Hub-Signature-256": sign_body(secret, body)}

    def test_token_only_requests(self):
        self.assertEqual(evaluate_push(None, {}, b"").action, "token")
        self.assertEqual(evaluate_push(_git(), {}, b"").action, "token")
        # A provider header on a non-git deployment is also a token-only request.
        self.assertEqual(evaluate_push(None, {"X-GitHub-Event": "push"}, b"").action, "token")

    def test_github_push_deploys(self):
        body = _push()
        d = evaluate_push(_git(), self._gh(body), body)
        self.assertEqual(d.action, "deploy", d.reason)
        self.assertEqual(d.provider, "github")
        self.assertEqual(d.commit, "a" * 40)

    def test_bad_signature(self):
        body = _push()
        self.assertEqual(evaluate_push(_git(), self._gh(body, secret="x"), body).action, "unauthorized")
        hdr = self._gh(body)
        self.assertEqual(evaluate_push(_git(), hdr, body + b" ").action, "unauthorized")

    def test_ping(self):
        body = b'{"zen":"hi"}'
        self.assertEqual(evaluate_push(_git(), self._gh(body, "ping"), body).action, "pong")

    def test_filters(self):
        body = _push(ref="refs/heads/dev")
        self.assertEqual(evaluate_push(_git(), self._gh(body), body).action, "ignore")
        body = _push(files=("docs/readme.md",))
        d = evaluate_push(_git(), self._gh(body), body)
        self.assertEqual((d.action, d.reason), ("ignore", "no changes under the compose directory"))
        body = _push()
        self.assertEqual(evaluate_push(_git(autoDeploy=False), self._gh(body), body).action, "ignore")
        body = _push()
        self.assertEqual(evaluate_push(_git(), self._gh(body, "issues"), body).action, "ignore")

    def test_no_file_list_deploys(self):
        body = json.dumps({"ref": "refs/heads/main", "after": "b" * 40}).encode()
        self.assertEqual(evaluate_push(_git(), self._gh(body), body).action, "deploy")

    def test_deleted_ref_ignored(self):
        body = json.dumps({"ref": "refs/heads/main", "after": "0" * 40, "commits": []}).encode()
        self.assertEqual(evaluate_push(_git(), self._gh(body), body).action, "ignore")

    def test_gitea(self):
        body = _push()
        hdr = {"X-Gitea-Event": "push", "X-Gitea-Signature": sign_body(SECRET, body)[7:]}
        self.assertEqual(evaluate_push(_git(), hdr, body).action, "deploy")

    def test_gitlab(self):
        body = _push()
        hdr = {"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": SECRET}
        self.assertEqual(evaluate_push(_git(), hdr, body).action, "deploy")
        hdr["X-Gitlab-Token"] = "wrong"
        self.assertEqual(evaluate_push(_git(), hdr, body).action, "unauthorized")

    def test_github_form_encoded(self):
        from urllib.parse import urlencode

        body = urlencode({"payload": _push().decode()}).encode()
        self.assertEqual(evaluate_push(_git(), self._gh(body), body).action, "deploy")


class TestEnv(unittest.TestCase):
    def test_parse_env_file(self):
        env = parse_env_file(
            "# c\nA=1\nexport B = two \nC=\"x y\"\nD='$lit'\nE=v # note\nbad line\n1X=no\n"
        )
        self.assertEqual(env, {"A": "1", "B": "two", "C": "x y", "D": "$lit", "E": "v"})

    def test_interpolate(self):
        env = {"TAG": "1.2", "EMPTY": ""}
        src = "image: app:${TAG}\nx: $TAG\ny: ${MISSING:-d}\nz: $$TAG\nw: ${EMPTY:-def}\nv: ${TAG:+set}"
        out = interpolate(src, env)
        self.assertEqual(
            out, "image: app:1.2\nx: 1.2\ny: ${MISSING:-d}\nz: $$TAG\nw: def\nv: set"
        )
        self.assertEqual(interpolate("a: ${X}", {}), "a: ${X}")


class TestStoreGitColumns(unittest.TestCase):
    def test_roundtrip(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        dep = Deployment(id="d1", name="g", type="compose", strategy="standalone",
                         targets=["h"], git=_git(), createdAt="c", updatedAt="u")
        store.create_deployment(dep)
        got = store.get_deployment("d1")
        self.assertEqual(got.git.repo_url, "https://example.com/o/r.git")
        self.assertEqual(got.git.webhook_secret, SECRET)
        got.deployed_commit = "c" * 40
        got.last_sync_at = "2026-10-25T00:00:00Z"
        got.git = None
        store.update_deployment(got)
        again = store.get_deployment("d1")
        self.assertIsNone(again.git)
        self.assertEqual(again.deployed_commit, "c" * 40)
        # JSON shape (camelCase) the frontend reads.
        js = _git().model_dump(by_alias=True)
        self.assertEqual(set(js), {"repoUrl", "ref", "path", "credentialId", "autoDeploy", "webhookSecret", "previews"})


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestCloneAndDeploy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.repo, "stack"))
        with open(os.path.join(self.repo, "stack", "docker-compose.yml"), "w") as f:
            f.write("services:\n  web:\n    image: nginx:${TAG}\n")
        with open(os.path.join(self.repo, "stack", ".env"), "w") as f:
            f.write("TAG=1.27\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, env=env)
        self.url = "file://" + self.repo
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                                   capture_output=True, text=True).stdout.strip()
        # Tests use a local file:// repo; production only allows network protocols.
        self._p = mock.patch.object(gitsource, "ALLOWED_PROTOCOLS", "file")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ls_remote_and_clone(self):
        async def go():
            sha = await gitsource.ls_remote(self.url, "main")
            self.assertEqual(sha, self.head)
            dest = os.path.join(self.tmp, "data", "git", "d1")
            self.assertEqual(await gitsource.clone(self.url, "main", dest), self.head)
            # Re-clone replaces the checkout in place.
            self.assertEqual(await gitsource.clone(self.url, "main", dest), self.head)
            raw, env = gitsource.read_compose(dest, "stack/docker-compose.yml")
            self.assertIn("${TAG}", raw)
            self.assertIn("nginx:1.27", gitsource.render_compose(raw, env))
            with self.assertRaises(gitsource.GitError):
                gitsource.read_compose(dest, "../../etc/passwd")
            with self.assertRaises(gitsource.GitError):
                await gitsource.ls_remote(self.url, "nope")
        asyncio.run(go())

    def test_service_deploy_records_commit(self):
        from kowan.deployment.service import STATUS_DEPLOYED, DeploymentService

        store = Store(os.path.join(self.tmp, "t.db"))
        dep = Deployment(id="d1", name="g", type="compose", strategy="standalone",
                         targets=["h1"], git=_git(repoUrl=self.url), createdAt="c", updatedAt="u")
        store.create_deployment(dep)

        class _Ops:
            def __init__(self):
                self.calls = []

            async def compose_up(self, project, yaml):
                self.calls.append((project, yaml))
                return ""

        ops = _Ops()

        class _Cfg:
            data_dir = os.path.join(self.tmp, "data")

        class _Svc:
            pass

        svc = _Svc()
        svc.store, svc.docker, svc.config = store, object(), _Cfg()
        dep_svc = DeploymentService(svc)
        dep_svc._ops_for_host = lambda host_id: (ops, None)  # type: ignore[assignment]

        async def go():
            await dep_svc.deploy("d1")
            got = store.get_deployment("d1")
            self.assertEqual(got.status, STATUS_DEPLOYED)
            self.assertEqual(got.deployed_commit, self.head)
            self.assertNotEqual(got.last_sync_at, "")
            self.assertIn("${TAG}", got.compose)  # raw file mirrored for display
            self.assertEqual(ops.calls[0][0], "g")
            self.assertIn("nginx:1.27", ops.calls[0][1])  # .env applied
            # Up to date: sync does not redeploy.
            _, changed, commit = await dep_svc.sync("d1")
            self.assertFalse(changed)
            self.assertEqual(commit, self.head)
        asyncio.run(go())


class TestGitDeploymentAPI(unittest.IsolatedAsyncioTestCase):
    """REST create/update + the push webhook on /hooks/deploy/{token}."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        from kowan.server import build_app
        from tests.test_integration import _cfg

        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        self.H = {"Authorization": "Bearer " + (await r.json())["token"]}
        self.dispatched = []

        async def fake_dispatch(dep_id, *, redeploy=False):
            self.dispatched.append(dep_id)
            return self.svc.store.get_deployment(dep_id)

        self.svc.deployments.dispatch_deploy = fake_dispatch
        # Deployments must target a host that exists.
        from kowan.models import Host

        self.svc.store.create_host(Host(id="h1", name="h1", type="agent", mode="edge", token="t" * 64,
                                        createdAt="2026-01-01T00:00:00Z"))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_create_and_push_webhook(self):
        body = {"name": "gitapp", "type": "compose", "strategy": "standalone", "targets": ["h1"],
                "compose": "", "git": {"repoUrl": "https://example.com/o/r.git", "ref": "",
                                       "path": "/stack/docker-compose.yml", "autoDeploy": True,
                                       "webhookSecret": "client-chosen"}}
        r = await self.client.post("/api/v1/deployments", headers=self.H, json=body)
        self.assertEqual(r.status, 201, await r.text())
        dep = await r.json()
        g = dep["git"]
        self.assertEqual((g["ref"], g["path"]), ("main", "stack/docker-compose.yml"))
        self.assertNotEqual(g["webhookSecret"], "client-chosen")
        self.assertTrue(dep["redeployToken"])
        url = "/hooks/deploy/" + dep["redeployToken"]
        secret = g["webhookSecret"]

        payload = _push()
        hdr = {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign_body(secret, payload),
               "Content-Type": "application/json"}
        r = await self.client.post(url, data=payload, headers=hdr)
        self.assertEqual(r.status, 202, await r.text())
        self.assertEqual(self.dispatched, [dep["id"]])

        hdr["X-Hub-Signature-256"] = sign_body("wrong", payload)
        r = await self.client.post(url, data=payload, headers=hdr)
        self.assertEqual(r.status, 401)

        other = _push(ref="refs/heads/dev")
        hdr["X-Hub-Signature-256"] = sign_body(secret, other)
        r = await self.client.post(url, data=other, headers=hdr)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["status"], "ignored")
        self.assertEqual(len(self.dispatched), 1)

        # Backward compatible: a plain POST with the path token still redeploys.
        r = await self.client.post(url)
        self.assertEqual(r.status, 202)
        self.assertEqual(len(self.dispatched), 2)

        # Update keeps the server-side secret; switching to inline drops git.
        body["git"]["ref"] = "dev"
        r = await self.client.put(f"/api/v1/deployments/{dep['id']}", headers=self.H, json=body)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["git"]["webhookSecret"], secret)
        inline = dict(body, git=None, compose="services: {}")
        r = await self.client.put(f"/api/v1/deployments/{dep['id']}", headers=self.H, json=inline)
        self.assertIsNone((await r.json())["git"])

    async def test_validation(self):
        base = {"name": "x", "type": "compose", "strategy": "standalone", "targets": ["h1"]}
        for git, msg in (
            ({"repoUrl": "file:///etc"}, "repoUrl"),
            ({"repoUrl": "https://e.com/r.git", "path": "../x.yml"}, "inside"),
        ):
            r = await self.client.post("/api/v1/deployments", headers=self.H, json=dict(base, git=git))
            self.assertEqual(r.status, 400)
            self.assertIn(msg, await r.text())
        r = await self.client.post("/api/v1/deployments", headers=self.H, json=base)
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/deployments", headers=self.H,
                                   json=dict(base, type="container", spec={"image": "a"},
                                             git={"repoUrl": "https://e.com/r.git"}))
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
