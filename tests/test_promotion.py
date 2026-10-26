"""Promotion (staging -> production): the planner for container, inline
compose and git deployments, the compose image editor, and the REST flow
(pairing, plan with diff, promote as a revision, redeploy, roles, errors)."""

import unittest

from kowan.deployment.promotion import compose_images, plan, replace_images
from kowan.errors import Validation
from kowan.models import ContainerSpec, Deployment, GitSource, KeyValue, PreviewInfo
from tests.apicase import ServerCase


def ctr(id_, image, **kw):
    return Deployment(id=id_, name=id_, type="container", strategy="standalone", targets=["local"],
                      spec=ContainerSpec(image=image, env=[KeyValue(key="MODE", value=id_)]), **kw)


def compose(id_, text):
    return Deployment(id=id_, name=id_, type="compose", strategy="standalone", targets=["local"], compose=text)


def gitdep(id_, commit="", repo="https://github.com/acme/shop.git", ref="main"):
    return Deployment(id=id_, name=id_, type="compose", strategy="standalone", targets=["local"],
                      git=GitSource(repoUrl=repo, ref=ref, autoDeploy=True), deployedCommit=commit)


STAGING = """services:
  web:
    image: ghcr.io/acme/web:2.0
  worker:
    image: ghcr.io/acme/worker:2.0
  extra:
    image: redis:7
"""
PROD = """# production
services:
  web:
    image: ghcr.io/acme/web:1.9   # keep this comment
    environment:
      MODE: prod
  worker:
    image: 'ghcr.io/acme/worker:1.9'
  db:
    image: postgres:16
"""


class TestImages(unittest.TestCase):
    def test_compose_images(self):
        self.assertEqual(compose_images(STAGING), {"web": "ghcr.io/acme/web:2.0", "worker": "ghcr.io/acme/worker:2.0", "extra": "redis:7"})
        self.assertEqual(compose_images(""), {})
        self.assertEqual(compose_images("services: [1]"), {})
        self.assertEqual(compose_images("services:\n  a: {build: .}\n"), {})
        with self.assertRaises(Validation):
            compose_images("services: [")

    def test_replace_keeps_the_file(self):
        out = replace_images(PROD, {"web": "ghcr.io/acme/web:2.0", "worker": "ghcr.io/acme/worker:2.0"})
        self.assertIn("    image: ghcr.io/acme/web:2.0   # keep this comment\n", out)
        self.assertIn("    image: 'ghcr.io/acme/worker:2.0'\n", out)
        self.assertIn("# production\n", out)
        self.assertIn("image: postgres:16", out)
        nested = "services:\n  a:\n    labels:\n      image: x\n    image: old\nx-other:\n  a:\n    image: no\n"
        self.assertEqual(replace_images(nested, {"a": "new"}),
                         "services:\n  a:\n    labels:\n      image: x\n    image: new\nx-other:\n  a:\n    image: no\n")
        self.assertEqual(replace_images("services:\r\n  a:\r\n    image: old\r\n", {"a": "new"}), "services:\r\n  a:\r\n    image: new\r\n")


class TestPlan(unittest.TestCase):
    def test_container(self):
        p = plan(ctr("staging", "api:2.1"), ctr("prod", "api:2.0"))
        self.assertEqual([c.as_dict() for c in p.changes], [{"what": "image", "from": "api:2.0", "to": "api:2.1"}])
        self.assertEqual(p.target.spec.image, "api:2.1")
        self.assertEqual(p.target.spec.env[0].value, "prod")  # the environment stays
        self.assertEqual(plan(ctr("s", "x"), ctr("p", "x")).changes, [])

    def test_compose(self):
        p = plan(compose("staging", STAGING), compose("prod", PROD))
        self.assertEqual([c.what for c in p.changes], ["web image", "worker image"])
        self.assertEqual(compose_images(p.target.compose)["web"], "ghcr.io/acme/web:2.0")
        self.assertIn("MODE: prod", p.target.compose)
        self.assertEqual(p.notes, ["not in prod, left out: extra"])
        # Flow style: rewritten through YAML.
        p = plan(compose("s", STAGING), compose("p", "services: {web: {image: 'ghcr.io/acme/web:1'}}"))
        self.assertEqual(compose_images(p.target.compose), {"web": "ghcr.io/acme/web:2.0"})
        self.assertIn("rewritten", p.notes[-1])

    def test_git(self):
        p = plan(gitdep("staging", "a" * 40, ref="develop"), gitdep("prod", "b" * 40, repo="https://GitHub.com/acme/shop/"))
        self.assertTrue(p.pinned)
        self.assertEqual((p.target.git.ref, p.target.git.auto_deploy), ("a" * 40, False))
        self.assertEqual(p.changes[0].as_dict(), {"what": "commit", "from": "b" * 40, "to": "a" * 40})
        same = gitdep("prod", "a" * 40, ref="a" * 40)
        self.assertEqual(plan(gitdep("staging", "a" * 40), same).changes, [])
        other_path = gitdep("prod")
        other_path.git.path = "prod/compose.yml"
        self.assertIn("reads prod/compose.yml", plan(gitdep("staging", "c" * 40), other_path).notes[0])

    def test_refusals(self):
        for src, dst, msg in (
            (ctr("a", "x"), ctr("a", "x"), "itself"),
            (ctr("a", "x"), compose("b", PROD), "container deployment"),
            (ctr("a", "x", preview=PreviewInfo(parentId="p")), ctr("b", "x"), "previews"),
            (gitdep("a", "c" * 40), compose("b", PROD), "same kind"),
            (gitdep("a", "c" * 40), gitdep("b", repo="https://github.com/acme/other.git"), "different repositories"),
            (gitdep("a"), gitdep("b"), "not been deployed"),
            (Deployment(id="a", type="container"), ctr("b", "x"), "container spec"),
        ):
            with self.assertRaises(Validation) as cm:
                plan(src, dst)
            self.assertIn(msg, str(cm.exception), msg)


class TestPromotionAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.dispatched = []

        async def fake_dispatch(dep_id, *, redeploy=False):
            self.dispatched.append(dep_id)
            return self.svc.store.get_deployment(dep_id)

        self.svc.deployments.dispatch_deploy = fake_dispatch

    async def make(self, name, **body):
        _, d = await self.call("POST", "/deployments", json={"name": name, "type": "compose", "strategy": "standalone",
                                                               "targets": ["local"], **body}, expect=201)
        return d

    async def test_pair_plan_and_promote(self):
        staging = await self.make("staging", compose=STAGING)
        prod = await self.make("prod", compose=PROD)
        sid, pid = staging["id"], prod["id"]
        await self.roles("PUT", f"/deployments/{sid}/promotion", "operator", json={"to": pid})
        _, out = await self.call("PUT", f"/deployments/{sid}/promotion", "operator", json={"to": pid}, expect=200)
        self.assertEqual(out["promoteTo"], pid)
        # Plans are readable by viewers.
        _, p = await self.call("GET", f"/deployments/{sid}/promote", "viewer", expect=200)
        self.assertEqual(p["target"], {"id": pid, "name": "prod"})
        self.assertEqual([c["what"] for c in p["changes"]], ["web image", "worker image"])
        self.assertTrue(any(line["op"] == "+" and "web:2.0" in line["text"] for line in p["diff"]))
        self.assertEqual(p["notes"], ["not in prod, left out: extra"])

        await self.roles("POST", f"/deployments/{sid}/promote", "operator")
        _, dep = await self.call("POST", f"/deployments/{sid}/promote", "operator", expect=200)
        self.assertIn("ghcr.io/acme/web:2.0   # keep this comment", self.svc.store.get_deployment(pid).compose)
        self.assertEqual(self.dispatched, [pid])
        _, revs = await self.call("GET", f"/deployments/{pid}/revisions", expect=200)
        self.assertEqual((revs[0]["source"], revs[0]["author"]), ("promote", "operator"))
        self.assertIn("from staging: web image ghcr.io/acme/web:2.0", revs[0]["note"])
        # Nothing left to promote.
        _, err = await self.call("POST", f"/deployments/{sid}/promote", expect=409)
        self.assertIn("already runs", err["error"])
        # Deleting the target unpairs the source.
        await self.call("DELETE", f"/deployments/{pid}", expect=200)
        self.assertEqual(self.svc.store.get_deployment(sid).promote_to, "")
        _, err = await self.call("GET", f"/deployments/{sid}/promote", expect=400)
        self.assertIn("pick the deployment", err["error"])

    async def test_explicit_target_no_deploy_and_errors(self):
        staging = await self.make("s2", compose=STAGING)
        prod = await self.make("p2", compose=PROD)
        sid, pid = staging["id"], prod["id"]
        await self.call("POST", f"/deployments/{sid}/promote", json={"to": pid, "deploy": False}, expect=200)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(compose_images(self.svc.store.get_deployment(pid).compose)["worker"], "ghcr.io/acme/worker:2.0")
        await self.call("GET", f"/deployments/{sid}/promote", params={"to": "missing"}, expect=404)
        await self.call("POST", f"/deployments/{sid}/promote", json={"to": pid, "extra": 1}, expect=400)
        await self.call("POST", f"/deployments/{sid}/promote", data=b"[1]", expect=400)
        _, api = await self.call("POST", "/deployments", json={"name": "api", "type": "container", "strategy": "standalone",
                                                                "targets": ["local"], "spec": {"image": "api:1"}}, expect=201)
        _, err = await self.call("PUT", f"/deployments/{sid}/promotion", json={"to": api["id"]}, expect=400)
        self.assertIn("container", err["error"])
        await self.call("PUT", f"/deployments/{sid}/promotion", json={"to": "missing"}, expect=404)
        await self.call("PUT", f"/deployments/{sid}/promotion", json={"to": pid, "x": 1}, expect=400)
        await self.call("PUT", f"/deployments/{sid}/promotion", data=b"[1]", expect=400)
        _, out = await self.call("PUT", f"/deployments/{sid}/promotion", json={"to": ""}, expect=200)
        self.assertEqual(out["promoteTo"], "")
        # A disabled target is saved but not deployed.
        await self.call("PUT", f"/deployments/{pid}/enabled", json={"enabled": False}, expect=200)
        _, api2 = await self.call("POST", "/deployments", json={"name": "api2", "type": "container", "strategy": "standalone",
                                                                 "targets": ["local"], "spec": {"image": "api:2"}}, expect=201)
        await self.call("PUT", f"/deployments/{api2['id']}/enabled", json={"enabled": False}, expect=200)
        await self.call("POST", f"/deployments/{api['id']}/promote", json={"to": api2["id"]}, expect=200)
        self.assertEqual(self.svc.store.get_deployment(api2["id"]).spec.image, "api:1")
        self.assertEqual(self.dispatched, [])

    async def test_git_promotion_pins_the_commit(self):
        git = {"repoUrl": "https://github.com/acme/shop.git", "ref": "main", "autoDeploy": True}
        staging = await self.make("gs", git=dict(git, ref="develop"))
        prod = await self.make("gp", git=git)
        d = self.svc.store.get_deployment(staging["id"])
        d.deployed_commit = "f" * 40
        self.svc.store.update_deployment(d)
        _, p = await self.call("GET", f"/deployments/{staging['id']}/promote", params={"to": prod["id"]}, expect=200)
        self.assertTrue(p["pinned"])
        _, dep = await self.call("POST", f"/deployments/{staging['id']}/promote", json={"to": prod["id"]}, expect=200)
        self.assertEqual((dep["git"]["ref"], dep["git"]["autoDeploy"]), ("f" * 40, False))
        _, revs = await self.call("GET", f"/deployments/{prod['id']}/revisions", expect=200)
        self.assertIn("commit ffffffffffff", revs[0]["note"])


if __name__ == "__main__":
    unittest.main()
