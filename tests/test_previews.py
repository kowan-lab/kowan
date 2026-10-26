"""Preview environments: branch and pull request events from GitHub, Gitea
and GitLab, compose isolation, a preview deployed from a local repository,
and the webhook / REST flow (create, update, close, delete, limits, expiry)."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import yaml

from kowan.deployment import gitsource, previews
from kowan.deployment.gitsource import sign_body
from kowan.deployment.previews import (
    branch_matches, branch_of, evaluate_event, isolate_compose, preview_env, preview_name, validate_settings,
)
from kowan.models import Deployment, GitSource, PreviewInfo, PreviewSettings
from kowan.store import Store
from tests.apicase import ServerCase

ON = PreviewSettings(enabled=True, branches="feature/*, fix-*", pullRequests=True, ttlHours=24, max=3)


def gh(event: str, payload: dict):
    return {"X-GitHub-Event": event}, json.dumps(payload).encode()


class TestHelpers(unittest.TestCase):
    def test_names_and_patterns(self):
        self.assertEqual(branch_of("refs/heads/feature/x"), "feature/x")
        self.assertEqual(branch_of("refs/tags/v1"), "")
        self.assertEqual(branch_of("dev"), "dev")
        self.assertTrue(branch_matches("feature/*, fix-*", "fix-42"))
        self.assertFalse(branch_matches("feature/*", "main"))
        self.assertFalse(branch_matches("", "x"))
        parent = Deployment(name="shop")
        self.assertEqual(preview_name(parent, "Feature/New UI!", 0), "shop-feature-new-ui")
        self.assertEqual(preview_name(parent, "x", 12), "shop-pr-12")
        self.assertEqual(preview_name(parent, "!!!", 0), "shop-branch")
        self.assertEqual(len(preview_name(parent, "a" * 80, 0)), len("shop-") + 30)

    def test_settings(self):
        self.assertEqual(validate_settings(ON), "")
        self.assertIn("ttlHours", validate_settings(PreviewSettings(ttlHours=-1)))
        self.assertIn("ttlHours", validate_settings(PreviewSettings(ttlHours=10_000)))
        self.assertIn("max", validate_settings(PreviewSettings(max=0)))
        self.assertIn("max", validate_settings(PreviewSettings(max=21)))
        self.assertIn("branch patterns", validate_settings(PreviewSettings(enabled=True, branches="", pullRequests=False)))
        self.assertEqual(validate_settings(PreviewSettings(enabled=False, branches="", pullRequests=False)), "")

    def test_isolate_compose(self):
        out = yaml.safe_load(isolate_compose(
            "services:\n"
            "  web:\n    image: nginx\n    container_name: shop-web\n"
            "    ports: ['8080:80', '127.0.0.1:8443:443/tcp', '9000', '[::1]:5353:53/udp']\n"
            "  api:\n    image: api\n    ports:\n      - target: 3000\n        published: 3000\n        host_ip: 0.0.0.0\n"
            "  worker: just-a-string\n"
            "volumes:\n  data: {}\n"))
        self.assertNotIn("container_name", out["services"]["web"])
        self.assertEqual(out["services"]["web"]["ports"], ["80", "443/tcp", "9000", "53/udp"])
        self.assertEqual(out["services"]["api"]["ports"], [{"target": 3000}])
        self.assertEqual(out["volumes"], {"data": {}})
        self.assertEqual(isolate_compose("version: '3'\n"), "version: '3'\n")
        self.assertEqual(isolate_compose(""), "")

    def test_preview_env(self):
        self.assertEqual(preview_env(Deployment(name="x")), "")
        env = preview_env(Deployment(name="shop-pr-3", preview=PreviewInfo(ref="feat", pullRequest=3)))
        self.assertIn("KOWAN_PREVIEW_NAME=shop-pr-3", env)
        self.assertIn("KOWAN_PREVIEW_BRANCH=feat", env)
        self.assertIn("KOWAN_PREVIEW_PR=3", env)


class TestEvents(unittest.TestCase):
    def ev(self, headers, body, settings=ON, main="main"):
        return evaluate_event(settings, main, headers, body)

    def test_pushes(self):
        e = self.ev(*gh("push", {"ref": "refs/heads/feature/a", "after": "a" * 40}))
        self.assertEqual((e.action, e.branch, e.reason), ("upsert", "feature/a", ""))
        e = self.ev(*gh("push", {"ref": "refs/heads/other", "after": "a" * 40}))
        self.assertEqual((e.action, e.reason), ("upsert", "update-only"))
        self.assertEqual(self.ev(*gh("push", {"ref": "refs/heads/main"})).reason, "the deployment's own branch")
        self.assertEqual(self.ev(*gh("push", {"ref": "refs/tags/v1"})).reason, "not a branch")
        self.assertEqual(self.ev(*gh("push", {"ref": "refs/heads/feature/a", "deleted": True})).action, "delete")
        self.assertEqual(self.ev(*gh("push", {"ref": "refs/heads/feature/a", "after": "0" * 40})).action, "delete")
        # GitLab push hook.
        e = self.ev({"X-Gitlab-Event": "Push Hook"}, json.dumps({"ref": "refs/heads/fix-1", "after": "b" * 40}).encode())
        self.assertEqual((e.action, e.branch), ("upsert", "fix-1"))
        self.assertEqual(self.ev(*gh("issues", {})).action, "ignore")

    def test_pull_requests(self):
        pr = {"number": 7, "pull_request": {"head": {"ref": "topic"}, "title": "New checkout", "html_url": "https://gh/pr/7"}}
        for action in ("opened", "synchronize", "reopened"):
            e = self.ev(*gh("pull_request", dict(pr, action=action)))
            self.assertEqual((e.action, e.branch, e.pr, e.title, e.url), ("upsert", "topic", 7, "New checkout", "https://gh/pr/7"))
        self.assertEqual(self.ev(*gh("pull_request", dict(pr, action="closed"))).action, "delete")
        self.assertEqual(self.ev(*gh("pull_request", dict(pr, action="labeled"))).action, "ignore")
        self.assertEqual(self.ev(*gh("pull_request", {"action": "opened"})).action, "ignore")
        # Gitea sends the same payload under its own header.
        self.assertEqual(self.ev({"X-Gitea-Event": "pull_request"}, json.dumps(dict(pr, action="opened")).encode()).pr, 7)
        mr = {"object_attributes": {"iid": 4, "source_branch": "mr-branch", "title": "MR", "url": "https://gl/mr/4"}}
        gl = {"X-Gitlab-Event": "Merge Request Hook"}
        for action, want in (("open", "upsert"), ("update", "upsert"), ("merge", "delete"), ("close", "delete"), ("approved", "ignore")):
            body = json.dumps({"object_attributes": dict(mr["object_attributes"], action=action)}).encode()
            self.assertEqual(self.ev(gl, body).action, want, action)
        off = PreviewSettings(enabled=True, branches="*", pullRequests=False)
        self.assertEqual(self.ev(*gh("pull_request", dict(pr, action="opened")), settings=off).reason, "pull request previews are off")

    def test_off_and_bad_payloads(self):
        self.assertEqual(self.ev(*gh("push", {}), settings=None).reason, "previews are off")
        self.assertEqual(self.ev(*gh("push", {}), settings=PreviewSettings()).reason, "previews are off")
        self.assertEqual(self.ev({"X-GitHub-Event": "push"}, b"{nope").reason, "payload is not JSON")
        self.assertEqual(self.ev({"X-GitHub-Event": "push"}, b"[1]").reason, "payload is not an object")


class TestPreviewDeploy(unittest.TestCase):
    """A preview deployed from a local repository gets an isolated compose file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        repo = os.path.join(self.tmp, "src")
        os.makedirs(repo)
        with open(os.path.join(repo, "docker-compose.yml"), "w") as f:
            f.write("services:\n  web:\n    image: nginx:${TAG}\n    container_name: shop-web\n"
                    "    ports: ['8080:80']\n    environment:\n      HOST: ${KOWAN_PREVIEW_NAME:-shop}.example.com\n")
        with open(os.path.join(repo, ".env"), "w") as f:
            f.write("TAG=1.27\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"], ["checkout", "-qb", "feature/a"]):
            subprocess.run(["git", *args], cwd=repo, check=True, env=env)
        self.url = "file://" + repo
        self._p = mock.patch.object(gitsource, "ALLOWED_PROTOCOLS", "file")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preview_compose_is_isolated(self):
        from kowan.deployment.service import DeploymentService

        store = Store(os.path.join(self.tmp, "t.db"))
        git = GitSource(repoUrl=self.url, ref="main", path="docker-compose.yml")
        store.create_deployment(Deployment(id="p", name="shop", type="compose", strategy="standalone", targets=["h1"],
                                           git=git, createdAt="c", updatedAt="u"))
        store.create_deployment(Deployment(
            id="v", name="shop-feature-a", type="compose", strategy="standalone", targets=["h1"],
            git=git.model_copy(update={"ref": "feature/a"}), createdAt="c", updatedAt="u",
            preview=PreviewInfo(parentId="p", ref="feature/a")))
        self.assertEqual(store.get_deployment("v").preview.parent_id, "p")

        calls = []

        class _Ops:
            async def compose_up(self, project, text):
                calls.append((project, yaml.safe_load(text)))
                return ""

        class _Svc:
            pass

        svc = _Svc()
        svc.store, svc.docker = store, object()
        svc.config = type("C", (), {"data_dir": os.path.join(self.tmp, "data")})()
        dep_svc = DeploymentService(svc)
        dep_svc._ops_for_host = lambda host_id: (_Ops(), None)  # type: ignore[assignment]

        async def go():
            await dep_svc.deploy("p")
            await dep_svc.deploy("v")
        asyncio.run(go())
        (_, parent), (project, prev) = calls
        self.assertEqual(parent["services"]["web"]["ports"], ["8080:80"])
        # Unknown here: left for compose on the host, which applies the default.
        self.assertEqual(parent["services"]["web"]["environment"]["HOST"], "${KOWAN_PREVIEW_NAME:-shop}.example.com")
        self.assertEqual(project, "shop-feature-a")
        web = prev["services"]["web"]
        self.assertEqual((web["image"], web["ports"]), ("nginx:1.27", ["80"]))
        self.assertNotIn("container_name", web)
        self.assertEqual(web["environment"]["HOST"], "shop-feature-a.example.com")
        store.close()


class TestPreviewAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.dispatched = []

        async def fake_dispatch(dep_id, *, redeploy=False):
            self.dispatched.append(dep_id)
            return self.svc.store.get_deployment(dep_id)

        self.svc.deployments.dispatch_deploy = fake_dispatch
        self.body = {"name": "shop", "type": "compose", "strategy": "standalone", "targets": ["local"],
                     "git": {"repoUrl": "https://example.com/o/shop.git", "ref": "main", "autoDeploy": True,
                             "previews": {"enabled": True, "branches": "feature/*", "pullRequests": True,
                                          "ttlHours": 24, "max": 3}}}
        _, self.parent = await self.call("POST", "/deployments", json=self.body, expect=201)
        self.url = "/hooks/deploy/" + self.parent["redeployToken"]
        self.secret = self.parent["git"]["webhookSecret"]

    async def hook(self, event, payload, provider="github"):
        body = json.dumps(payload).encode()
        if provider == "gitlab":
            hdr = {"X-Gitlab-Event": event, "X-Gitlab-Token": self.secret}
        else:
            hdr = {"X-GitHub-Event": event, "X-Hub-Signature-256": sign_body(self.secret, body)}
        r = await self.client.post(self.url, data=body, headers={**hdr, "Content-Type": "application/json"})
        return r.status, await r.json()

    async def previews(self, role="admin"):
        _, items = await self.call("GET", f"/deployments/{self.parent['id']}/previews", role, expect=200)
        return {p["name"]: p for p in items}

    async def test_webhook_lifecycle(self):
        # A push to a matching branch creates and deploys a preview.
        status, out = await self.hook("push", {"ref": "refs/heads/feature/cart", "after": "a" * 40})
        self.assertEqual((status, out["status"]), (202, "preview-deploying"))
        p = (await self.previews())["shop-feature-cart"]
        self.assertEqual((p["preview"]["ref"], p["preview"]["parentId"], p["preview"]["createdBy"]),
                         ("feature/cart", self.parent["id"], "push"))
        self.assertEqual((p["git"]["ref"], p["git"]["autoDeploy"], p["git"].get("previews")), ("feature/cart", False, None))
        self.assertEqual(self.dispatched, [p["id"]])
        # Pushing again redeploys it; a non-matching branch without a preview is ignored.
        await self.hook("push", {"ref": "refs/heads/feature/cart", "after": "b" * 40})
        self.assertEqual(self.dispatched, [p["id"], p["id"]])
        status, out = await self.hook("push", {"ref": "refs/heads/random", "after": "a" * 40})
        self.assertEqual((status, out["status"]), (200, "ignored"))
        self.assertIn("does not match", out["reason"])
        # The main branch still deploys the parent itself.
        status, out = await self.hook("push", {"ref": "refs/heads/main", "after": "c" * 40})
        self.assertEqual((status, out["status"]), (202, "redeploying"))

        # A pull request gets its own preview; a push to its branch updates it.
        pr = {"number": 9, "pull_request": {"head": {"ref": "topic"}, "title": "Faster search", "html_url": "https://gh/9"}}
        await self.hook("pull_request", dict(pr, action="opened"))
        prev = (await self.previews())["shop-pr-9"]
        self.assertEqual((prev["preview"]["pullRequest"], prev["preview"]["title"]), (9, "Faster search"))
        n = len(self.dispatched)
        await self.hook("push", {"ref": "refs/heads/topic", "after": "d" * 40})
        self.assertEqual(self.dispatched[n:], [prev["id"]])
        # Closing the pull request and deleting the branch remove them.
        status, out = await self.hook("pull_request", dict(pr, action="closed"))
        self.assertEqual((status, out["status"], out["count"]), (202, "preview-removed", 1))
        await self.hook("push", {"ref": "refs/heads/feature/cart", "deleted": True, "after": "0" * 40})
        self.assertEqual(await self.previews(), {})

        # GitLab merge requests too; a bad signature is refused before anything.
        mr = {"object_attributes": {"iid": 2, "source_branch": "mr", "title": "MR", "url": "u", "action": "open"}}
        status, _ = await self.hook("Merge Request Hook", mr, provider="gitlab")
        self.assertEqual(status, 202)
        self.assertIn("shop-pr-2", await self.previews())
        r = await self.client.post(self.url, data=b"{}", headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=00"})
        self.assertEqual(r.status, 401)

    async def test_manual_previews_limits_and_roles(self):
        pid = self.parent["id"]
        await self.roles("POST", f"/deployments/{pid}/previews", "operator", json={"branch": "x"})
        _, dep = await self.call("POST", f"/deployments/{pid}/previews", "operator", json={"branch": "release/2"}, expect=201)
        self.assertEqual((dep["name"], dep["preview"]["createdBy"]), ("shop-release-2", "operator"))
        # Viewers see previews without secrets.
        seen = (await self.previews("viewer"))["shop-release-2"]
        self.assertEqual(seen["redeployToken"], "")
        for body, status in (({}, 400), ({"branch": " "}, 400), ({"branch": "main"}, 400), ({"branch": "x", "y": 1}, 400), (["x"], 400)):
            await self.call("POST", f"/deployments/{pid}/previews", json=body, expect=status)
        await self.call("POST", f"/deployments/{dep['id']}/previews", json={"branch": "z"}, expect=400)
        # The same branch again redeploys the existing preview.
        _, again = await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "release/2"}, expect=201)
        self.assertEqual(again["id"], dep["id"])
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "b2"}, expect=201)
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "b3"}, expect=201)
        _, err = await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "b4"}, expect=409)
        self.assertIn("3 previews", err["error"])
        # Over the limit, a webhook push is answered but ignored.
        status, out = await self.hook("push", {"ref": "refs/heads/feature/z", "after": "a" * 40})
        self.assertEqual((status, out["status"]), (200, "ignored"))
        # A name taken by another deployment.
        _, other = await self.call("POST", "/deployments", json={"name": "shop-taken", "type": "compose", "strategy": "standalone",
                                                                 "targets": ["local"], "compose": "services: {}"}, expect=201)
        await self.call("DELETE", f"/deployments/{again['id']}", expect=200)
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "taken"}, expect=409)
        # Deleting the parent removes its previews.
        await self.call("DELETE", f"/deployments/{pid}", expect=200)
        names = {d.name for d in self.svc.store.list_deployments()}
        self.assertEqual(names, {other["name"]})

    async def test_settings_validation(self):
        bad = json.loads(json.dumps(self.body))
        bad["name"] = "other"
        for previews_cfg, msg in (({"enabled": True, "ttlHours": -1}, "ttlHours"),
                                  ({"enabled": True, "max": 50}, "max"),
                                  ({"enabled": True, "branches": "", "pullRequests": False}, "branch patterns")):
            bad["git"]["previews"] = previews_cfg
            _, err = await self.call("POST", "/deployments", json=bad, expect=400)
            self.assertIn(msg, err["error"])
        bad["git"]["previews"] = {"enabled": True, "branches": "*"}
        bad["strategy"] = "swarm"
        _, err = await self.call("POST", "/deployments", json=bad, expect=400)
        self.assertIn("standalone", err["error"])
        # Previews off: the manual route refuses, webhooks ignore other branches.
        off = json.loads(json.dumps(self.body))
        off["git"]["previews"]["enabled"] = False
        await self.call("PUT", f"/deployments/{self.parent['id']}", json=off, expect=200)
        _, err = await self.call("POST", f"/deployments/{self.parent['id']}/previews", json={"branch": "x"}, expect=400)
        self.assertIn("not enabled", err["error"])
        status, out = await self.hook("push", {"ref": "refs/heads/feature/a", "after": "a" * 40})
        self.assertEqual((status, out["status"]), (200, "ignored"))

    async def test_expiry(self):
        pid = self.parent["id"]
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "old"}, expect=201)
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "new"}, expect=201)
        mgr = self.svc.previews
        now = datetime.now(timezone.utc)
        self.assertEqual(await mgr.expire(now), 0)
        old = next(d for d in mgr.list(pid) if d.preview.ref == "old")
        old.preview.last_activity_at = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.svc.store.update_deployment(old)
        self.assertEqual(await mgr.expire(now), 1)
        self.assertEqual([d.preview.ref for d in mgr.list(pid)], ["new"])
        # Turning previews off on the parent removes the rest.
        off = json.loads(json.dumps(self.body))
        off["git"]["previews"] = {"enabled": False, "ttlHours": 0}
        await self.call("PUT", f"/deployments/{pid}", json=off, expect=200)
        self.assertEqual(await mgr.expire(now), 1)
        self.assertEqual(mgr.list(pid), [])
        # ttl 0 keeps previews forever.
        on = json.loads(json.dumps(self.body))
        on["git"]["previews"]["ttlHours"] = 0
        await self.call("PUT", f"/deployments/{pid}", json=on, expect=200)
        await self.call("POST", f"/deployments/{pid}/previews", json={"branch": "forever"}, expect=201)
        self.assertEqual(await mgr.expire(now + timedelta(days=365)), 0)
        # The background loop runs expire and survives errors.
        with mock.patch.object(previews, "_EXPIRE_EVERY", 0.01):
            mgr2 = previews.PreviewManager(self.svc)
            calls = []

            async def boom(now=None):
                calls.append(1)
                raise RuntimeError("x")

            mgr2.expire = boom
            mgr2.start()
            await asyncio.sleep(0.05)
            await mgr2.stop()
            await mgr2.stop()
        self.assertGreater(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
